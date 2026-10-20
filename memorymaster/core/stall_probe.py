"""Observation-only probes that attribute prompt-hook latency stalls (T-1648).

T-1646 measured a long tail on the prompt hook: 7% of requests exceed the 1.5 s
client timeout, and in the slowest ones hydration and render inflate together.
A per-stage timer cannot tell "this stage was slow" from "this whole process was
not running", so each dense recall event also carries the facts that can:

* ``lag_ms``: worst overshoot of a 100 ms heartbeat thread during the request.
  The thread needs the GIL and a CPU slice to wake, so lag means the process (not
  one stage) was starved or paused.
* ``cpu_ms`` against ``wall_ms``: CPU time of the request thread. A large gap is
  waiting (I/O, page faults, GIL, scheduler), not computing.
* ``pf`` and ``ws_mb``: page faults taken by the process during the request and its
  working set at the start. A working set far below the committed memory means the
  next request pays to fault its code and heap back in.
* ``gc_n`` / ``gc_ms``: collections and total pause during the request.
* ``q_wait_ms``: time spent behind the server's request limiter.
* ``conn_n`` / ``conn_ms``: SQLite connections opened by the request thread.
* ``wal_kb`` / ``wal_shrunk``: write-ahead log size, and whether it shrank since the
  previous request (a checkpoint ran in between).
* ``prio``: CPU class, I/O priority and memory priority of the process.

Nothing here changes what recall returns, and nothing raises: a counter that
cannot be read is left out. ``MEMORYMASTER_STALL_PROBE=0`` turns every probe off.
Standard library only (ctypes on Windows); the hook process imports it too.
"""
from __future__ import annotations

import gc
import os
import sys
import threading
import time
from collections import deque
from typing import Any


ENV_SWITCH = "MEMORYMASTER_STALL_PROBE"

HEARTBEAT_INTERVAL_S = 0.1
# Windows timers tick every 15.6 ms, so a 100 ms sleep routinely wakes 15 ms late.
LAG_FLOOR_MS = 50.0
LAG_LOG_MS = 500.0

_local = threading.local()


def _log(event: str, *, warning: bool = False, **fields: Any) -> None:
    """Structured log line; imported lazily so the per-prompt hook process stays stdlib-light."""
    try:
        import logging

        from memorymaster.core.structured_log import log_event

        log_event(logging.getLogger("memorymaster.stall_probe"), event,
                  level=logging.WARNING if warning else logging.INFO, **fields)
    except Exception:  # noqa: BLE001 - observation only
        pass


def enabled() -> bool:
    return os.environ.get(ENV_SWITCH, "1").strip().lower() not in {"0", "false", "no", "off"}


# -- per-thread request facts ------------------------------------------------------------------


def note_connect(seconds: float) -> None:
    """Count one SQLite connection opened by the calling thread (``connect_ro``)."""
    _local.conn_n = getattr(_local, "conn_n", 0) + 1
    _local.conn_s = getattr(_local, "conn_s", 0.0) + seconds


def note_lock_wait(seconds: float) -> None:
    """Add time the calling thread spent waiting for a lock it does not own (the dense encoder lock)."""
    _local.lock_s = (getattr(_local, "lock_s", None) or 0.0) + seconds


def worker_started(enqueued_at: float) -> None:
    """Called first on the worker thread; ``enqueued_at`` is ``perf_counter`` when the request arrived."""
    _local.queue_wait_ms = max(0.0, (time.perf_counter() - enqueued_at) * 1000.0)


def worker_finished() -> None:
    _local.queue_wait_ms = None


def queue_wait_ms() -> float | None:
    return getattr(_local, "queue_wait_ms", None)


# -- garbage collector -------------------------------------------------------------------------

_GC_LOCK = threading.Lock()
_GC_STATE = {"installed": False, "n": 0, "n2": 0, "s": 0.0, "t0": 0.0}


def _on_gc(phase: str, info: dict[str, Any]) -> None:
    if phase == "start":
        _GC_STATE["t0"] = time.perf_counter()
        return
    _GC_STATE["n"] += 1
    if info.get("generation") == 2:
        _GC_STATE["n2"] += 1
    _GC_STATE["s"] += max(0.0, time.perf_counter() - _GC_STATE["t0"])


def _gc_totals() -> tuple[int, int, float]:
    with _GC_LOCK:
        if not _GC_STATE["installed"]:
            gc.callbacks.append(_on_gc)
            _GC_STATE["installed"] = True
    return _GC_STATE["n"], _GC_STATE["n2"], _GC_STATE["s"]


# -- operating system counters (Windows only; elsewhere they are simply absent) -----------------

_PRIORITY_CLASSES = {0x20: "normal", 0x40: "idle", 0x80: "high", 0x100: "realtime",
                     0x4000: "below_normal", 0x8000: "above_normal"}
_IO_PRIORITIES = {0: "very_low", 1: "low", 2: "normal", 3: "high", 4: "critical"}
_WAL_LAST: dict[str, int] = {}
# Request stages that do not overlap, so wall time minus their sum is time no stage explains.
DISJOINT_STAGES = ("svc_ms", "plan_ms", "seen_ms", "rtt_ms", "hydrate_ms", "render_ms", "jev_ms", "remember_ms",
                   "access_ms")


_WIN: dict[str, Any] = {}


def _kernel32():
    """``(ctypes, kernel32, PROCESS_MEMORY_COUNTERS)``, built once."""
    if not _WIN:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.GetPriorityClass.restype = ctypes.c_uint32
        k32.GetPriorityClass.argtypes = [ctypes.c_void_p]

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

        k32.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), wintypes.DWORD]
        _WIN.update(ctypes=ctypes, k32=k32, counters=Counters)
    return _WIN["ctypes"], _WIN["k32"], _WIN["counters"]


def _memory_counters() -> tuple[int, int] | None:
    """``(page_fault_count, working_set_bytes)`` of this process, or ``None`` off Windows."""
    if sys.platform != "win32":
        return None
    ctypes, k32, Counters = _kernel32()
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if not k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return None
    return int(counters.PageFaultCount), int(counters.WorkingSetSize)


def process_priority(pid: int | None = None) -> dict[str, Any]:
    """CPU class, I/O priority and memory priority (1 lowest to 5 normal) of ``pid`` (default: this process)."""
    if sys.platform != "win32":
        return {}
    ctypes, k32, _ = _kernel32()
    handle = k32.GetCurrentProcess() if pid is None else k32.OpenProcess(0x1000, 0, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return {}
    out: dict[str, Any] = {}
    try:
        out["cls"] = _PRIORITY_CLASSES.get(int(k32.GetPriorityClass(handle)), "unknown")
        io = ctypes.c_uint32(99)
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtQueryInformationProcess.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                                    ctypes.c_uint32, ctypes.c_void_p]
        if ntdll.NtQueryInformationProcess(handle, 33, ctypes.byref(io), 4, None) == 0:  # ProcessIoPriority
            out["io"] = _IO_PRIORITIES.get(io.value, str(io.value))
        mem = ctypes.c_uint32(0)
        k32.GetProcessInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        if k32.GetProcessInformation(handle, 0, ctypes.byref(mem), 4):  # ProcessMemoryPriority
            out["mem"] = int(mem.value)
    finally:
        if pid is not None:
            k32.CloseHandle(handle)
    return out


def process_age_ms() -> float | None:
    """Milliseconds since this process was created: interpreter start plus imports, as the OS saw it."""
    if sys.platform != "win32" or not enabled():
        return None
    try:
        ctypes, k32, _ = _kernel32()
        from ctypes import wintypes

        created, other = wintypes.FILETIME(), wintypes.FILETIME()
        k32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        if not k32.GetProcessTimes(k32.GetCurrentProcess(), ctypes.byref(created), ctypes.byref(other),
                                   ctypes.byref(other), ctypes.byref(other)):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return round(max(0.0, (time.time() - (ticks / 1e7 - 11644473600)) * 1000.0), 1)
    except Exception:  # noqa: BLE001 - observation only
        return None


_PRIO_CACHE: list[str] = []


def _prio_label() -> str | None:
    if not _PRIO_CACHE:
        try:
            prio = process_priority()
            _PRIO_CACHE.append(f"{prio.get('cls', '?')}/io:{prio.get('io', '?')}/mem:{prio.get('mem', '?')}" if prio else "")
        except Exception:  # noqa: BLE001
            _PRIO_CACHE.append("")
    return _PRIO_CACHE[0] or None


# -- heartbeat ---------------------------------------------------------------------------------


class Heartbeat:
    """Wakes every ``interval_s`` and keeps the overshoot of each wake above the noise floor."""

    def __init__(self, interval_s: float = HEARTBEAT_INTERVAL_S) -> None:
        self.interval_s = interval_s
        self.ticks = 0
        self.worst_ms = 0.0
        self._expected: float | None = None
        self._lags: deque[tuple[float, float]] = deque(maxlen=1024)  # (expected, woke) of each late wake
        self._stop = threading.Event()

    def observe(self, now: float) -> None:
        if self._expected is not None:
            lag_ms = (now - self._expected) * 1000.0
            if lag_ms >= LAG_FLOOR_MS:
                self._lags.append((self._expected, now))
                self.worst_ms = max(self.worst_ms, lag_ms)
                if lag_ms >= LAG_LOG_MS:
                    self._log_stall(lag_ms)
        self.ticks += 1
        self._expected = now + self.interval_s

    def _log_stall(self, lag_ms: float) -> None:
        memory = None
        try:
            memory = _memory_counters()
        except Exception:  # noqa: BLE001
            pass
        label = f"process_stall lag_ms={lag_ms:.0f}"  # the dense service's plain-text log shows only the message
        _log(label, warning=True, lag_ms=round(lag_ms, 1),
             pf_total=memory[0] if memory else None, ws_mb=round(memory[1] / 2**20) if memory else None,
             threads=threading.active_count())

    def max_lag_ms(self, start: float, end: float) -> float:
        """Longest stretch of ``[start, end]`` during which the heartbeat was overdue.

        A late wake was overdue from the tick it should have made until it woke; only the part inside the window
        counts. A tick still pending at ``end`` is counted too: a stall in flight has not woken yet, so it is in
        no list, and it is the one the request most needs to see.
        """
        spans = [(expected, woke) for expected, woke in list(self._lags)]
        expected = self._expected
        if expected is not None and end > expected:
            spans.append((expected, end))
        overlaps = [(min(woke, end) - max(expected, start)) * 1000.0 for expected, woke in spans]
        worst = max((ms for ms in overlaps if ms >= LAG_FLOOR_MS), default=0.0)
        return round(worst, 1)

    def run(self) -> None:
        self.observe(time.perf_counter())
        while not self._stop.wait(self.interval_s):
            self.observe(time.perf_counter())

    def stop(self) -> None:
        self._stop.set()


_HEARTBEAT: Heartbeat | None = None


def start_heartbeat(interval_s: float = HEARTBEAT_INTERVAL_S) -> Heartbeat | None:
    """Start the process-wide heartbeat thread once (the shared server calls this at boot)."""
    global _HEARTBEAT
    if not enabled():
        return None
    if _HEARTBEAT is None:
        beat = Heartbeat(interval_s)
        threading.Thread(target=beat.run, name="stall-heartbeat", daemon=True).start()
        _HEARTBEAT = beat
        _log("stall_probe_started", interval_s=interval_s, prio=_prio_label())
    return _HEARTBEAT


# -- one request -------------------------------------------------------------------------------


class Window:
    """Counters at construction; :meth:`finish` returns what changed since."""

    def __init__(self, db_path: str | None = None) -> None:
        self.on = enabled()
        if not self.on:
            return
        self.db_path = db_path
        _local.conn_n, _local.conn_s, _local.lock_s = 0, 0.0, None
        self.wall0 = time.perf_counter()
        self.cpu0 = time.thread_time()
        self.gc0: tuple[int, int, float] | None = None
        self.mem0: tuple[int, int] | None = None
        try:
            self.gc0 = _gc_totals()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.mem0 = _memory_counters()
        except Exception:  # noqa: BLE001
            pass

    def finish(self, **stages: Any) -> dict[str, Any]:
        if not self.on:
            return {}
        end = time.perf_counter()
        out: dict[str, Any] = {k: v for k, v in stages.items() if v is not None}
        out["wall_ms"] = round((end - self.wall0) * 1000.0, 1)
        if any(name in out for name in DISJOINT_STAGES):
            out["other_ms"] = round(max(0.0, out["wall_ms"] - sum(out.get(name, 0.0) for name in DISJOINT_STAGES)), 1)
        out["cpu_ms"] = round((time.thread_time() - self.cpu0) * 1000.0, 1)
        out["threads"] = threading.active_count()
        out["conn_n"] = getattr(_local, "conn_n", 0)
        out["conn_ms"] = round(getattr(_local, "conn_s", 0.0) * 1000.0, 1)
        try:
            n, n2, seconds = _gc_totals()
            out["gc_n"] = n - (self.gc0[0] if self.gc0 else n)
            out["gc2_n"] = n2 - (self.gc0[1] if self.gc0 else n2)
            out["gc_ms"] = round((seconds - (self.gc0[2] if self.gc0 else seconds)) * 1000.0, 1)
        except Exception:  # noqa: BLE001
            pass
        try:
            memory = _memory_counters()
            if memory and self.mem0:
                out["pf"] = (memory[0] - self.mem0[0]) % (1 << 32)
                out["ws_mb"] = round(self.mem0[1] / 2**20, 1)
        except Exception:  # noqa: BLE001
            pass
        if (lock_s := getattr(_local, "lock_s", None)) is not None:
            out["lock_ms"] = round(lock_s * 1000.0, 1)
        if (wait := queue_wait_ms()) is not None:
            out["q_wait_ms"] = round(wait, 1)
        if _HEARTBEAT is not None:
            out["lag_ms"] = _HEARTBEAT.max_lag_ms(self.wall0, end)
        if self.db_path:
            try:
                size = os.stat(f"{self.db_path}-wal").st_size
                out["wal_kb"] = round(size / 1024, 1)
                out["wal_shrunk"] = size < _WAL_LAST.get(self.db_path, size)
                _WAL_LAST[self.db_path] = size
            except OSError:
                pass
        if (prio := _prio_label()) is not None:
            out["prio"] = prio
        return out
