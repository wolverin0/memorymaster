"""T-1648: observation-only probes that attribute prompt-hook latency stalls.

T-1646 measured that in 44 of the 50 slowest dense recalls hydration and render
inflated together (7% of hook requests exceed the 1.5 s client timeout) and that
no scheduled job, idle gap or concurrent request explained it. Per-stage timers
alone cannot separate "the stage was slow" from "the whole process was not
running", so these probes add the discriminating facts to each dense event:
heartbeat lag, thread CPU against wall time, page faults, GC pauses, queue wait
and connection acquire time. They must never change recall output or raise.
"""
from __future__ import annotations

import gc
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

from memorymaster.core import stall_probe
from memorymaster.stores._storage_shared import connect_ro

WINDOWS = sys.platform == "win32"

WINDOW_KEYS = {"wall_ms", "cpu_ms", "gc_n", "gc_ms", "threads"}
STAGE_KEYS = {"svc_ms", "plan_ms", "rtt_ms", "hydrate_ms", "claim_ms", "claims_n", "render_ms", "access_ms"}


@pytest.fixture(autouse=True)
def _probe_env(monkeypatch):
    monkeypatch.delenv(stall_probe.ENV_SWITCH, raising=False)


def test_window_reports_wall_cpu_gc_and_threads() -> None:
    window = stall_probe.Window()
    gc.collect()
    time.sleep(0.05)
    fields = window.finish()
    assert WINDOW_KEYS <= set(fields)
    assert fields["wall_ms"] >= 45
    assert fields["cpu_ms"] < fields["wall_ms"]  # sleeping is waiting, not computing
    assert fields["gc_n"] >= 1 and fields["gc_ms"] >= 0
    assert fields["threads"] >= 1


def test_time_no_stage_explains_is_reported_as_other(monkeypatch) -> None:
    """A stall outside every timed stage must show up as a gap, not vanish into the wall clock."""
    window = stall_probe.Window()
    time.sleep(0.12)
    fields = window.finish(rtt_ms=10.0, hydrate_ms=20.0, claim_ms=15.0)  # claim_ms is inside hydrate_ms
    assert fields["wall_ms"] >= 115
    assert fields["other_ms"] == pytest.approx(fields["wall_ms"] - 30.0, abs=0.2)
    assert "other_ms" not in stall_probe.Window().finish()  # no stages given, nothing to subtract


def test_window_counts_connection_opens_made_on_its_thread(tmp_path) -> None:
    db = tmp_path / "m.db"
    sqlite3.connect(db).close()
    window = stall_probe.Window()
    for _ in range(3):
        connect_ro(db).close()
    fields = window.finish()
    assert fields["conn_n"] == 3 and fields["conn_ms"] >= 0
    # another window starts from zero: the counters are per thread and per window
    assert stall_probe.Window().finish()["conn_n"] == 0


def test_queue_wait_is_the_time_between_enqueue_and_worker_start() -> None:
    assert stall_probe.queue_wait_ms() is None  # not behind the server's limiter
    stall_probe.worker_started(time.perf_counter() - 0.25)
    try:
        assert 240 <= stall_probe.queue_wait_ms() < 5000
        assert stall_probe.Window().finish()["q_wait_ms"] >= 240
    finally:
        stall_probe.worker_finished()
    assert stall_probe.queue_wait_ms() is None
    assert "q_wait_ms" not in stall_probe.Window().finish()


def test_heartbeat_keeps_only_lag_above_the_noise_floor() -> None:
    beat = stall_probe.Heartbeat(interval_s=0.1)
    beat.observe(10.0)  # first tick only arms the schedule
    beat.observe(10.1)  # on time
    beat.observe(10.2 + 0.030)  # 30 ms of Windows timer jitter is noise
    beat.observe(10.33 + 0.9)  # a 900 ms stall
    assert beat.ticks == 4
    assert beat.max_lag_ms(0.0, 11.3) == pytest.approx(900, abs=1)
    assert beat.max_lag_ms(10.0, 10.3) == 0.0  # the stall woke at 11.23, outside this window
    assert beat.worst_ms == pytest.approx(900, abs=1)


def test_a_heartbeat_still_frozen_at_the_end_of_the_window_reports_the_overdue_time() -> None:
    """The stalls worth catching are the ones in flight when the request finishes: no wake-up is recorded yet."""
    beat = stall_probe.Heartbeat(interval_s=0.1)
    beat.observe(10.0)  # armed: next wake expected at 10.1, and it never comes
    assert beat.max_lag_ms(10.0, 10.05) == 0.0  # not yet overdue inside this window
    assert beat.max_lag_ms(10.0, 10.9) == pytest.approx(800, abs=1)  # overdue since 10.1


def test_a_recorded_stall_counts_only_for_its_overlap_with_the_window() -> None:
    beat = stall_probe.Heartbeat(interval_s=0.1)
    beat.observe(10.0)
    beat.observe(11.1)  # woke 1.0 s late: overdue from 10.1 to 11.1
    assert beat.max_lag_ms(10.3, 10.4) == pytest.approx(100, abs=1)  # the window sat inside the stall
    assert beat.max_lag_ms(10.0, 10.2) == pytest.approx(100, abs=1)  # the stall began mid-window
    assert beat.max_lag_ms(11.0, 11.15) == pytest.approx(100, abs=1)  # and ended mid-window
    beat.observe(11.2)  # ticking again
    assert beat.max_lag_ms(11.15, 11.3) == 0.0  # afterwards


def test_window_sees_a_heartbeat_that_froze_during_the_request(monkeypatch) -> None:
    beat = stall_probe.Heartbeat(interval_s=0.01)
    monkeypatch.setattr(stall_probe, "_HEARTBEAT", beat)
    window = stall_probe.Window()
    beat.observe(window.wall0)  # the thread ticked once and then froze with the process
    time.sleep(0.15)
    assert window.finish()["lag_ms"] >= 100


def test_heartbeat_thread_runs_and_stops() -> None:
    beat = stall_probe.Heartbeat(interval_s=0.01)
    thread = threading.Thread(target=beat.run, daemon=True)
    thread.start()
    time.sleep(0.15)
    beat.stop()
    thread.join(2)
    assert not thread.is_alive() and beat.ticks >= 3


def test_window_includes_heartbeat_lag_only_while_a_heartbeat_runs(monkeypatch) -> None:
    assert "lag_ms" not in stall_probe.Window().finish()
    beat = stall_probe.Heartbeat(interval_s=0.001)
    monkeypatch.setattr(stall_probe, "_HEARTBEAT", beat)
    window = stall_probe.Window()
    beat.observe(window.wall0)
    beat.observe(window.wall0 + 0.06)  # woke 59 ms late, inside the window
    time.sleep(0.07)
    assert window.finish()["lag_ms"] >= 55


def test_window_reports_wal_size_and_a_checkpoint_between_requests(tmp_path) -> None:
    db = tmp_path / "m.db"
    wal = Path(str(db) + "-wal")
    wal.write_bytes(b"x" * 4096)
    first = stall_probe.Window(str(db)).finish()
    assert first["wal_kb"] == 4.0 and first["wal_shrunk"] is False
    wal.write_bytes(b"")  # a checkpoint reset the log between two requests
    second = stall_probe.Window(str(db)).finish()
    assert second["wal_kb"] == 0.0 and second["wal_shrunk"] is True


@pytest.mark.skipif(not WINDOWS, reason="page faults and priorities are read through kernel32")
def test_windows_counters_and_priorities_are_readable() -> None:
    fields = stall_probe.Window().finish()
    assert isinstance(fields["pf"], int) and fields["pf"] >= 0
    assert fields["ws_mb"] > 0
    prio = stall_probe.process_priority()
    assert {"cls", "io", "mem"} <= set(prio) and prio["cls"] in {"normal", "below_normal", "above_normal", "idle", "high"}
    assert stall_probe.process_age_ms() > 0  # a long CI run is already an hour old


def test_a_failing_os_counter_never_breaks_the_window(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise OSError("no kernel32 here")

    monkeypatch.setattr(stall_probe, "_memory_counters", boom)
    monkeypatch.setattr(stall_probe, "_gc_totals", boom)
    fields = stall_probe.Window().finish()
    assert "wall_ms" in fields and "pf" not in fields


def test_the_switch_turns_every_probe_off(monkeypatch) -> None:
    monkeypatch.setenv(stall_probe.ENV_SWITCH, "0")
    assert stall_probe.Window().finish() == {}
    assert stall_probe.start_heartbeat() is None


# -- the dense service ------------------------------------------------------------------------


def _fake_ml(monkeypatch, gate: threading.Event | None = None):
    """Hermetic torch and sentence-transformers: encode() blocks on ``gate`` so a second caller must queue."""
    import types

    import numpy as np

    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch.bfloat16, torch.float32 = "bf16", "f32"

    class FakeModel:
        def __init__(self, *_a, **_k):
            self.max_seq_length = 0

        def encode(self, texts, **_k):
            if gate is not None:
                gate.wait(5)
            return np.ones((len(texts), 768), dtype=np.float32)

    st = types.ModuleType("sentence_transformers")
    st.SentenceTransformer = FakeModel
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "sentence_transformers", st)


def test_the_encoder_reports_how_long_a_query_waited_for_the_model_lock(monkeypatch) -> None:
    """The index sync embeds claims under the same lock as the queries: a query can queue behind a batch."""
    from memorymaster.recall import dense_server

    gate = threading.Event()
    _fake_ml(monkeypatch, gate)
    encode, _device = dense_server.load_encoder(768, "cpu")
    holder = threading.Thread(target=encode, args=(["a"], "document"), daemon=True)
    holder.start()
    time.sleep(0.05)
    waited: list[float | None] = []

    def query() -> None:
        window = stall_probe.Window()
        encode(["q"], "query")
        waited.append(window.finish().get("lock_ms"))

    follower = threading.Thread(target=query, daemon=True)
    follower.start()
    time.sleep(0.2)
    gate.set()
    holder.join(5)
    follower.join(5)
    assert waited and waited[0] >= 150


def test_importing_the_probe_keeps_the_hook_process_lean() -> None:
    """The prompt hook imports this on every prompt; T-1646 measured its whole start-up at about 75 ms."""
    import subprocess

    code = ("import sys, memorymaster.core.hook_log, memorymaster.core.stall_probe; "
            "bad = [m for m in ('logging', 'structlog', 'memorymaster.core.structured_log') if m in sys.modules]; "
            "print(','.join(bad)); sys.exit(1 if bad else 0)")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, f"stall_probe pulled in: {done.stderr or done.stdout}"

