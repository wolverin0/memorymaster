"""Supervisor + launcher for the ONE shared local MemoryMaster MCP server (T-0726).

Every client session reaches the server through the stdio relay
(memorymaster.surfaces.mcp_stdio_proxy), which declares the client's workspace.

Default mode (what the MemoryMaster-MCP-Shared scheduled task runs) is a tiny
stdlib-only supervisor: one instance per user, it starts the server as a child
(`--serve`), probes /healthz and restarts the child when it exits or stops
answering, so a dead server never stays dead. A child that never answers at all is killed after
FIRST_ANSWER_DEADLINE_SECONDS (growing for consecutive ones); a stuck kill never freezes supervision.
The child exits with its supervisor.

Configuration lives only in HKCU\\Software\\MemoryMaster\\SharedMcp, so the token is
never in a command line or a file; Hermes (team mode, own key) is untouched. The
supervisor refuses to start without the token and both allowlists: any token holder
picks a workspace and db per call, so the allowlists are the scope boundary.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SHARED_KEY = r"Software\MemoryMaster\SharedMcp"
PORT = 8766
HEALTH_URL = f"http://127.0.0.1:{PORT}/healthz"
REQUIRED = (
    "MEMORYMASTER_MCP_HTTP_TOKEN",
    "MEMORYMASTER_MCP_WORKSPACE_ALLOWLIST",
    "MEMORYMASTER_MCP_DB_ALLOWLIST",
    "MEMORYMASTER_DEFAULT_DB",
)
OPTIONAL = ("MEMORYMASTER_LOG_DIR",)
STARTUP_GRACE_SECONDS = 300  # model pre-import + first bind can take minutes on a busy box (measured max 297 s)
# A child that has not answered /healthz at all is killed and relaunched after this many seconds, then 2x, 4x...
# capped at STARTUP_GRACE_SECONDS for each consecutive one that also never answers. 2026-10-13: a child that never
# bound the port lived 19 min. Healthy boots take 9-297 s (median 47) and the host stalls in storms, so a fixed 60 s
# would kill slow-but-alive boots forever (54 restarts on 2026-10-11); a child that answers resets the ladder.
FIRST_ANSWER_DEADLINE_SECONDS = 60
PROBE_EVERY_SECONDS = 10
PROBE_FAILURES_BEFORE_RESTART = 3
# A paged-out server on a RAM-starved box stops answering for ~30 s and then recovers;
# killing it costs a ~95 s restart (torch pre-import). Kill only a sustained outage (2026-10-03).
UNHEALTHY_SECONDS_BEFORE_RESTART = 120
# A wedged server can ignore TerminateProcess for minutes (2026-10-11: 54 replacements were spawned over one
# live orphan that held the port, ~75 min without a server). No new child starts until the old pid is dead.
KILL_WAIT_SECONDS = 30
TASKKILL_TIMEOUT_SECONDS = 30
TASKKILL_ABANDON_GRACE_SECONDS = 15  # subprocess.run kills a timed-out taskkill then waits for it with no timeout
KILL_RETRY_BACKOFF_SECONDS = 15
_LAST_PROBE_FAILURE = ""  # why the latest /healthz probe failed: timeout | refused | http <code> | <ExcName>
_TASKKILL_WORKER: threading.Thread | None = None  # a taskkill call that has not returned yet; at most one at a time


def _log_stream():
    configured = os.environ.get("MEMORYMASTER_LOG_DIR", "").strip()
    base = Path(configured) if configured else Path(os.environ["LOCALAPPDATA"]) / "MemoryMaster" / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return (base / "mcp-shared.log").open("a", encoding="utf-8", buffering=1)


def _say(message: str, level: str = "info") -> None:
    # Same JSON-lines shape as the server's records in this log (memorymaster/core/structured_log.py).
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "level": level,
                      "logger": "memorymaster.mcp.supervisor", "component": "mcp-supervisor",
                      "event": message, "pid": os.getpid()}), flush=True)


def _load_shared_environment() -> list[str]:
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, SHARED_KEY)
    except OSError:
        return list(REQUIRED)
    with key:
        for name in REQUIRED + OPTIONAL:
            try:
                value, _kind = winreg.QueryValueEx(key, name)
            except OSError:
                continue
            if isinstance(value, str) and value.strip():
                os.environ[name] = value.strip()
    # One process serves every local session; team identity belongs to Hermes only.
    os.environ["MEMORYMASTER_MCP_AUTH_MODE"] = "local-trusted"
    for name in ("MEMORYMASTER_MCP_PRINCIPAL", "MEMORYMASTER_MCP_TENANT_ID", "MEMORYMASTER_MCP_ALLOWED_SCOPES"):
        os.environ.pop(name, None)
    return [name for name in REQUIRED if not os.environ.get(name)]


def _single_instance() -> bool:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, "Local\\MemoryMasterMcpSharedSupervisor")
    globals()["_MUTEX"] = handle  # keep the handle for the process lifetime
    return bool(handle) and ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def _probe_failure_reason(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"http {exc.code}"
    reason = getattr(exc, "reason", exc)  # urlopen wraps socket errors in URLError
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(reason, ConnectionRefusedError):
        return "refused"
    return type(reason).__name__


def _healthy() -> bool:
    global _LAST_PROBE_FAILURE
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as response:
            if response.status == 200:
                return True
            _LAST_PROBE_FAILURE = f"http {response.status}"
            return False
    except Exception as exc:  # noqa: BLE001 - any failure is "not healthy"
        _LAST_PROBE_FAILURE = _probe_failure_reason(exc)
        return False


def _kill_pid(pid: int) -> None:
    """Hard-kill ``pid`` and its whole process tree (``taskkill /F /T``); never raises, never blocks for long.

    ``subprocess.run(timeout=)`` kills a timed-out taskkill and then waits for it with no timeout, so a taskkill
    stuck in the same stall would freeze supervision (2026-10-13: 57 s for a 30 s timeout). The call runs on a
    worker thread that is abandoned after the timeout plus a margin; one blocked worker at a time is ever left.
    """
    global _TASKKILL_WORKER
    if _TASKKILL_WORKER is not None and _TASKKILL_WORKER.is_alive():
        _say(f"an earlier taskkill is still blocked; not starting another for pid {pid}", "error")
        return

    def work() -> None:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F", "/T"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=TASKKILL_TIMEOUT_SECONDS, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # pythonw: no console flash
            )
        except subprocess.TimeoutExpired:
            _say(f"taskkill for pid {pid} TIMED OUT after {TASKKILL_TIMEOUT_SECONDS}s", "error")
        except Exception as exc:  # noqa: BLE001 - the caller re-checks liveness and retries
            _say(f"taskkill for pid {pid} failed: {exc!r}", "error")

    _TASKKILL_WORKER = threading.Thread(target=work, name="taskkill", daemon=True)
    _TASKKILL_WORKER.start()
    _TASKKILL_WORKER.join(TASKKILL_TIMEOUT_SECONDS + TASKKILL_ABANDON_GRACE_SECONDS)
    if _TASKKILL_WORKER.is_alive():
        _say(f"taskkill for pid {pid} is blocked past its own timeout; abandoning it", "error")


def _wait_dead(child, seconds: float) -> bool:
    try:
        child.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        pass
    return child.poll() is not None


def _stop_child(child) -> None:
    """Return only when ``child`` is confirmed dead; a live orphan keeps the port, so never give up.

    kill -> bounded wait -> ``taskkill /F /T`` -> re-check; if it is STILL alive, log it, back off
    and run the kill loop again instead of letting the caller spawn a second server. Every step is
    bounded, so supervision keeps turning (and logging) while an orphan refuses to die.
    """
    first_kill, rounds = time.monotonic(), 0
    while True:
        if child.poll() is not None:
            return
        rounds += 1
        try:
            child.kill()
        except OSError as exc:  # access denied / already gone: fall through to taskkill + the liveness re-check
            _say(f"kill of server pid {child.pid} raised {exc!r}; escalating")
        if _wait_dead(child, KILL_WAIT_SECONDS):
            return
        _say(f"server pid {child.pid} did not exit after kill; escalating to taskkill /F /T")
        _kill_pid(child.pid)
        if _wait_dead(child, KILL_WAIT_SECONDS):
            return
        _say(f"server pid {child.pid} is STILL alive after taskkill (round {rounds}, "
             f"{time.monotonic() - first_kill:.0f}s since the first kill); not starting a new server, "
             f"retrying in {KILL_RETRY_BACKOFF_SECONDS}s", "error")
        time.sleep(KILL_RETRY_BACKOFF_SECONDS)


def _supervise() -> int:
    if not _single_instance():
        _say("another supervisor is already running; exiting")
        return 0
    missing = _load_shared_environment()
    if missing:
        _say(f"refusing to start, missing {', '.join(missing)} in HKCU\\{SHARED_KEY}")
        return 2
    os.chdir(Path(os.environ["MEMORYMASTER_DEFAULT_DB"]).parent)  # relative tool paths never land in System32
    backoff, silent_children = 5.0, 0
    while True:
        started = time.monotonic()
        child = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--serve", "--parent-pid", str(os.getpid())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        first_answer_deadline = min(FIRST_ANSWER_DEADLINE_SECONDS * 2 ** silent_children, STARTUP_GRACE_SECONDS)
        _say(f"started server pid {child.pid}")
        seen_healthy, failures, unhealthy_since = False, 0, None
        while child.poll() is None:
            time.sleep(PROBE_EVERY_SECONDS)
            if _healthy():
                seen_healthy, failures, unhealthy_since, silent_children = True, 0, None, 0
                continue
            if not seen_healthy:  # wall clock since Popen, not a probe count: a starved loop must not stretch it
                waited = time.monotonic() - started
                if waited < first_answer_deadline:
                    continue
                _say(f"server pid {child.pid} never answered /healthz in {waited:.0f}s "
                     f"(deadline {first_answer_deadline}s, last: {_LAST_PROBE_FAILURE or 'unknown'}); killing it")
                silent_children += 1
                _stop_child(child)
                break
            failures += 1
            if unhealthy_since is None:
                unhealthy_since = time.monotonic()
            outage = time.monotonic() - unhealthy_since + PROBE_EVERY_SECONDS
            if failures >= PROBE_FAILURES_BEFORE_RESTART and outage >= UNHEALTHY_SECONDS_BEFORE_RESTART:
                _say(f"server pid {child.pid} failed {failures} health probes "
                     f"(last: {_LAST_PROBE_FAILURE or 'unknown'}); killing it")
                _stop_child(child)
                break
        _say(f"server pid {child.pid} exited with {child.returncode}")
        backoff = 5.0 if time.monotonic() - started > 600 else min(backoff * 2, 60.0)
        time.sleep(backoff)


def _exit_with_parent(parent_pid: int) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE
    if not handle:
        os._exit(0)  # supervisor already gone

    def wait() -> None:
        kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0xFFFFFFFF)
        os._exit(0)

    threading.Thread(target=wait, name="exit-with-supervisor", daemon=True).start()


def _serve(parent_pid: int) -> int:
    _exit_with_parent(parent_pid)
    db = os.environ["MEMORYMASTER_DEFAULT_DB"]
    from memorymaster.surfaces.mcp_http import main

    # --workspace only feeds /readyz; tool calls take the client's declared workspace.
    return main(["--host", "127.0.0.1", "--port", str(PORT), "--db", db, "--workspace", str(Path(db).parent)])


if __name__ == "__main__":
    stream = _log_stream()
    sys.stdout = stream
    sys.stderr = stream  # pythonw starts with no stderr: logging handlers must see the real stream
    try:
        if "--serve" in sys.argv:
            raise SystemExit(_serve(int(sys.argv[sys.argv.index("--parent-pid") + 1])))
        raise SystemExit(_supervise())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - last resort: never die silently under pythonw
        import traceback

        _say(f"fatal: {exc!r}")
        traceback.print_exc()
        raise SystemExit(1) from exc
