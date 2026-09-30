"""Supervisor + launcher for the ONE shared local MemoryMaster MCP server (T-0726).

Every client session reaches the server through the stdio relay
(memorymaster.surfaces.mcp_stdio_proxy), which declares the client's workspace.

Default mode (what the MemoryMaster-MCP-Shared scheduled task runs) is a tiny
stdlib-only supervisor: one instance per user, it starts the server as a child
(`--serve`), probes /healthz and restarts the child when it exits or stops
answering, so a dead server never stays dead. The child exits with its supervisor.

Configuration lives only in HKCU\\Software\\MemoryMaster\\SharedMcp, so the token is
never in a command line or a file; Hermes (team mode, own key) is untouched. The
supervisor refuses to start without the token and both allowlists: any token holder
picks a workspace and db per call, so the allowlists are the scope boundary.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
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
STARTUP_GRACE_SECONDS = 300  # model pre-import + first bind can take minutes on a busy box
PROBE_EVERY_SECONDS = 10
PROBE_FAILURES_BEFORE_RESTART = 3


def _log_stream():
    configured = os.environ.get("MEMORYMASTER_LOG_DIR", "").strip()
    base = Path(configured) if configured else Path(os.environ["LOCALAPPDATA"]) / "MemoryMaster" / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return (base / "mcp-shared.log").open("a", encoding="utf-8", buffering=1)


def _say(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} memorymaster-mcp-shared[{os.getpid()}]: {message}", flush=True)


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


def _healthy() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 - any failure is "not healthy"
        return False


def _supervise() -> int:
    if not _single_instance():
        _say("another supervisor is already running; exiting")
        return 0
    missing = _load_shared_environment()
    if missing:
        _say(f"refusing to start, missing {', '.join(missing)} in HKCU\\{SHARED_KEY}")
        return 2
    os.chdir(Path(os.environ["MEMORYMASTER_DEFAULT_DB"]).parent)  # relative tool paths never land in System32
    backoff = 5.0
    while True:
        started = time.monotonic()
        child = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--serve", "--parent-pid", str(os.getpid())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _say(f"started server pid {child.pid}")
        seen_healthy, failures = False, 0
        while child.poll() is None:
            time.sleep(PROBE_EVERY_SECONDS)
            if _healthy():
                seen_healthy, failures = True, 0
                continue
            if not seen_healthy and time.monotonic() - started < STARTUP_GRACE_SECONDS:
                continue
            failures += 1
            if failures >= PROBE_FAILURES_BEFORE_RESTART:
                _say(f"server pid {child.pid} failed {failures} health probes; killing it")
                child.kill()
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    _say(f"server pid {child.pid} did not exit after kill; starting a new one anyway")
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
