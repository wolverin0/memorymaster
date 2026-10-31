"""Out-of-process embedding model for the dense-recall service, loaded on demand and dropped when idle.

Measured 2026-10-30 on the 91 h dense service: the process held 6.6 GB of committed private memory
(``PrivateMemorySize64``) for a 22 MB index. ``import torch`` alone commits 1.6 GB and a loaded model
3.7-4.6 GB, and neither ``del model`` nor ``cuda.empty_cache()`` gives it back. The only way to return
that memory is to end the process that holds it, so the service keeps just the index and HTTP server
(no torch) and runs the model in a child process:

* started by the first query, never by the background index sync;
* ended after ``idle_s`` seconds without a query (the sync does not count as activity);
* the child exits by itself when the service dies (its stdin closes).

Protocol: one JSON object per line on the child's stdin/stdout. Vectors travel as base64 float32.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np

from memorymaster.core import stall_probe

# Model load measured at 5-8 s; a child silent for this long is killed so the service never hangs on it.
EXCHANGE_TIMEOUT_S = 120.0
CREATE_NO_WINDOW = 0x08000000


class EncoderUnavailable(RuntimeError):
    """The model child failed to start or died mid-request."""


class EncoderUnloaded(RuntimeError):
    """A background caller asked for an embedding while the model is unloaded; it must not load it."""


class WorkerEncoder:
    """``Encoder`` (``(texts, kind) -> vectors``) backed by a lazily started, idle-unloaded child process."""

    def __init__(self, dim: int, device: str | None, *, idle_s: float = 600.0, log_path: Path | None = None,
                 command: Sequence[str] | None = None) -> None:
        self._command = list(command) if command else self._default_command(dim, device)
        self._log_path = log_path
        self._idle_s = idle_s
        self._lock = threading.Lock()  # one exchange at a time, and load/unload never overlap one
        self._proc: subprocess.Popen[bytes] | None = None
        self._last_query = 0.0
        self.device = device or "auto"
        self.loads = 0
        self.unloads = 0
        self._stop = threading.Event()
        if idle_s > 0:
            threading.Thread(target=self._reap_loop, name="dense-idle-unload", daemon=True).start()

    @staticmethod
    def _default_command(dim: int, device: str | None) -> list[str]:
        exe = sys.executable
        if exe.lower().endswith("pythonw.exe"):  # a console child with no window keeps its std handles valid
            exe = exe[:-len("pythonw.exe")] + "python.exe"
        command = [exe, "-m", "memorymaster.recall.dense_worker", "--dim", str(dim)]
        return command + ["--device", device] if device else command

    @property
    def loaded(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def __call__(self, texts: Sequence[str], kind: str) -> np.ndarray:
        queued = time.perf_counter()
        with self._lock:
            stall_probe.note_lock_wait(time.perf_counter() - queued)  # T-1648: queries queue behind index-sync batches
            if kind != "query" and not self.loaded:
                raise EncoderUnloaded
            self._ensure_started()
            if kind == "query":
                self._last_query = time.monotonic()
            reply = self._exchange({"texts": list(texts), "kind": kind})
            if kind == "query":
                self._last_query = time.monotonic()
        if "error" in reply:
            raise EncoderUnavailable(str(reply["error"]))
        return np.frombuffer(base64.b64decode(reply["vectors"]), dtype=np.float32).reshape(reply["shape"]).copy()

    def _ensure_started(self) -> None:
        if self.loaded:
            return
        self._proc = None
        stderr: Any = subprocess.DEVNULL
        if self._log_path is not None:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            stderr = self._log_path.open("ab")
        env = {**os.environ, "TQDM_DISABLE": "1", "HF_HUB_DISABLE_PROGRESS_BARS": "1"}
        try:
            self._proc = subprocess.Popen(
                self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, env=env,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError as exc:
            raise EncoderUnavailable(type(exc).__name__) from exc
        finally:
            if stderr is not subprocess.DEVNULL:
                stderr.close()  # the child holds its own handle
        ready = self._read_line()
        if not ready.get("ready"):
            self._kill()
            raise EncoderUnavailable(str(ready.get("error") or "worker did not report ready"))
        self.device = str(ready.get("device") or self.device)
        self.loads += 1

    def _read_line(self) -> dict[str, Any]:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        watchdog = threading.Timer(EXCHANGE_TIMEOUT_S, proc.kill)
        watchdog.start()
        try:
            line = proc.stdout.readline()
        finally:
            watchdog.cancel()
        if not line:
            self._kill()
            raise EncoderUnavailable("worker exited")
        try:
            return json.loads(line)
        except ValueError as exc:
            self._kill()
            raise EncoderUnavailable("worker sent a malformed reply") from exc

    def _exchange(self, request: dict[str, Any]) -> dict[str, Any]:
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        try:
            proc.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
            proc.stdin.flush()
        except OSError as exc:
            self._kill()
            raise EncoderUnavailable("worker pipe closed") from exc
        return self._read_line()

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        if proc.poll() is None:
            proc.kill()
        proc.wait()

    def unload(self) -> bool:
        """End the child if it is idle; returns whether one was running."""
        if not self._lock.acquire(blocking=False):
            return False  # an exchange is in flight: not idle
        try:
            was_loaded = self.loaded
            self._kill()
            if was_loaded:
                self.unloads += 1
            return was_loaded
        finally:
            self._lock.release()

    def _reap_loop(self) -> None:
        interval = max(0.05, min(self._idle_s / 4, 30.0))
        while not self._stop.wait(interval):
            if self.loaded and time.monotonic() - self._last_query >= self._idle_s:
                self.unload()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self._kill()


def serve(encode: Callable[[Sequence[str], str], np.ndarray], stdin: BinaryIO, proto: BinaryIO) -> None:
    """The child's request loop; ends when the service closes our stdin."""
    for line in stdin:
        try:
            request = json.loads(line)
            vectors = np.ascontiguousarray(encode(request["texts"], request["kind"]), dtype=np.float32)
            reply: dict[str, Any] = {"vectors": base64.b64encode(vectors.tobytes()).decode("ascii"),
                                     "shape": list(vectors.shape)}
        except Exception as exc:  # noqa: BLE001 - reported to the service, which falls back to lexical recall
            reply = {"error": type(exc).__name__}
        proto.write(json.dumps(reply).encode("utf-8") + b"\n")
        proto.flush()


def run(load: Callable[[], tuple[Callable[[Sequence[str], str], np.ndarray], str]]) -> None:
    # Anything a library prints must not corrupt the protocol: keep the real stdout for ourselves.
    proto = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    try:
        encode, device = load()
    except Exception as exc:  # noqa: BLE001
        proto.write(json.dumps({"error": type(exc).__name__}).encode("utf-8") + b"\n")
        proto.flush()
        raise
    proto.write(json.dumps({"ready": True, "device": device}).encode("utf-8") + b"\n")
    proto.flush()
    with os.fdopen(os.dup(0), "rb") as stdin:
        serve(encode, stdin, proto)


def main(argv: Sequence[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="MemoryMaster dense-recall model worker")
    parser.add_argument("--dim", type=int, required=True)
    parser.add_argument("--device", default=None)
    args = parser.parse_args(argv)
    from memorymaster.recall import dense_server  # imports torch: only this process pays for it

    run(lambda: dense_server.load_encoder(args.dim, args.device))


if __name__ == "__main__":
    main()
