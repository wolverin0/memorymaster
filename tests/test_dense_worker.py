"""Dense-recall model child: lazy start, idle unload, and an index sync that never loads the model.

Background (2026-10-30): the dense service held 6.6 GB of committed memory for 91 h because torch's
commit is never returned in-process; the model now lives in a child process the service can end.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

from memorymaster.recall import dense_server
from memorymaster.recall.dense_worker import EncoderUnavailable, EncoderUnloaded, WorkerEncoder

# A stand-in for the real child: same protocol loop, a tiny deterministic "model", no torch.
FAKE_CHILD = (
    "import numpy as np, os, sys\n"
    "from memorymaster.recall import dense_worker\n"
    "def load():\n"
    "    def encode(texts, kind):\n"
    "        if 'die' in texts: os._exit(3)\n"
    "        return np.array([[len(t), 1.0, 0.0, 0.0] for t in texts], dtype=np.float32)\n"
    "    return encode, 'fake'\n"
    "dense_worker.run(load)\n"
)


def _encoder(idle_s: float = 0.0) -> WorkerEncoder:
    return WorkerEncoder(4, "cpu", idle_s=idle_s, command=[sys.executable, "-c", FAKE_CHILD])


def _alive(encoder: WorkerEncoder) -> bool:
    return encoder._proc is not None and encoder._proc.poll() is None


def test_the_model_process_is_not_started_until_the_first_query() -> None:
    encoder = _encoder()
    try:
        assert not encoder.loaded and encoder.loads == 0
        vectors = encoder(["abc", "de"], "query")
        assert vectors.shape == (2, 4) and vectors[0][0] == 3.0 and vectors[1][0] == 2.0
        assert encoder.loaded and encoder.loads == 1 and encoder.device == "fake"
    finally:
        encoder.close()


def test_a_background_embedding_never_starts_the_model() -> None:
    encoder = _encoder()
    try:
        with pytest.raises(EncoderUnloaded):
            encoder(["new claim"], "document")
        assert not encoder.loaded and encoder.loads == 0
    finally:
        encoder.close()


def test_the_model_process_ends_after_the_idle_period_and_comes_back_on_the_next_query() -> None:
    encoder = _encoder(idle_s=0.4)
    try:
        encoder(["a"], "query")
        child = encoder._proc
        deadline = time.monotonic() + 10
        while encoder.loaded and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not encoder.loaded and encoder.unloads == 1
        assert child is not None and child.poll() is not None  # the process is really gone
        assert encoder(["abcd"], "query")[0][0] == 4.0
        assert encoder.loads == 2
    finally:
        encoder.close()


def test_only_queries_count_as_activity_not_the_index_sync() -> None:
    encoder = _encoder(idle_s=0.6)
    try:
        encoder(["a"], "query")
        stamp = encoder._last_query
        time.sleep(0.2)
        encoder(["doc"], "document")  # a sync batch while the model is loaded
        assert encoder._last_query == stamp
        deadline = time.monotonic() + 10
        while encoder.loaded and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not encoder.loaded  # the sync did not keep it alive
    finally:
        encoder.close()


def test_concurrent_first_queries_start_one_process() -> None:
    encoder = _encoder()
    results: list[float] = []
    try:
        threads = [threading.Thread(target=lambda: results.append(encoder(["abc"], "query")[0][0])) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert results == [3.0] * 4 and encoder.loads == 1
    finally:
        encoder.close()


def test_a_child_that_dies_mid_request_fails_that_query_and_the_next_one_restarts_it() -> None:
    encoder = _encoder()
    try:
        encoder(["a"], "query")
        with pytest.raises(EncoderUnavailable):
            encoder(["die"], "query")
        assert not encoder.loaded
        assert encoder(["ab"], "query")[0][0] == 2.0 and encoder.loads == 2
    finally:
        encoder.close()


def test_closing_the_service_ends_the_child() -> None:
    encoder = _encoder()
    encoder(["a"], "query")
    child = encoder._proc
    encoder.close()
    assert child is not None and child.poll() is not None


@pytest.fixture()
def claims_db(tmp_path):
    path = tmp_path / "claims.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE claims (id INTEGER PRIMARY KEY, scope TEXT, text TEXT, status TEXT)")
    conn.executemany("INSERT INTO claims VALUES (?,?,?,?)",
                     [(1, "project:a", "alpha", "confirmed"), (2, "project:a", "beta", "confirmed")])
    conn.commit()
    conn.close()
    return path


def test_the_index_sync_does_not_embed_while_the_model_is_unloaded(claims_db) -> None:
    calls: list[int] = []

    def encode(texts, kind):
        calls.append(len(texts))
        return np.ones((len(texts), 4), dtype=np.float32)

    loaded = [True]
    service = dense_server.DenseService(claims_db, encode, model="fake", dim=4, device="cpu",
                                        is_loaded=lambda: loaded[0])
    assert service.sync_once() == 2 and calls == [2]

    loaded[0] = False
    conn = sqlite3.connect(claims_db)
    conn.execute("INSERT INTO claims VALUES (3,'project:a','gamma','confirmed')")
    conn.execute("UPDATE claims SET text='beta changed' WHERE id=2")
    conn.execute("UPDATE claims SET status='archived' WHERE id=1")
    conn.commit()
    conn.close()
    assert service.sync_once() == 0 and calls == [2]  # nothing embedded, the model was not touched
    assert service.index.ids.tolist() == [2]  # the removal still applied; the changed row keeps its old vector

    loaded[0] = True  # a query loaded the model: the next sync catches up
    assert service.sync_once() == 2
    assert sorted(service.index.ids.tolist()) == [2, 3]


def test_the_service_process_does_not_import_torch() -> None:
    """The 1.6 GB of commit charge comes from importing torch; only the model child may do it."""
    code = ("import sys, memorymaster.recall.dense_server; "
            "bad = [m for m in ('torch', 'sentence_transformers') if m in sys.modules]; "
            "print(','.join(bad)); sys.exit(1 if bad else 0)")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout


def test_the_service_keeps_numpy_to_one_blas_thread() -> None:
    """numpy's OpenBLAS commits ~750 MB of per-thread buffers at import; the service does one small matvec."""
    code = ("import os; os.environ.pop('OPENBLAS_NUM_THREADS', None); import memorymaster.recall.dense_server; "
            "print(os.environ.get('OPENBLAS_NUM_THREADS'))")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.stdout.strip() == "1", done.stderr


def _cold_service(claims_db, encoder: WorkerEncoder) -> dense_server.DenseService:
    service = dense_server.DenseService(claims_db, encoder, model="fake", dim=4, device="cpu",
                                        is_loaded=lambda: encoder.loaded, warm=encoder.warm)
    encoder(["alpha"], "query")  # load once so the index can embed, then drop the model: a cold service
    service.index.sync([(1, "project:a", "alpha")])
    assert encoder.unload() and not encoder.loaded
    return service


def _wait_loaded(encoder: WorkerEncoder) -> None:
    deadline = time.monotonic() + 20
    while not encoder.loaded and time.monotonic() < deadline:
        time.sleep(0.02)
    assert encoder.loaded


def test_a_query_on_an_unloaded_model_answers_cold_at_once_and_loads_in_the_background(claims_db) -> None:
    """Regression 2026-10-31: the first query after an idle unload waited for the 5-8 s load and hit the
    hook's 1.5 s deadline; it must answer immediately and let the load run behind it."""
    encoder = _encoder()
    try:
        service = _cold_service(claims_db, encoder)
        started = time.perf_counter()
        answer = service.search("hello", ["project:a"], 5)
        assert answer["cold"] is True and "results" not in answer
        assert time.perf_counter() - started < 0.5  # no model load on the request path
        _wait_loaded(encoder)
        assert encoder.loads == 2  # the setup load plus exactly one background load
        warm = service.search("hello", ["project:a"], 5)
        assert "cold" not in warm and warm["results"]  # the next query is served by the loaded model
    finally:
        encoder.close()


def test_concurrent_cold_queries_start_one_load(claims_db) -> None:
    encoder = _encoder()
    try:
        service = _cold_service(claims_db, encoder)
        assert all(service.search("q", None, 5).get("cold") for _ in range(5))
        _wait_loaded(encoder)
        assert encoder.loads == 2  # the setup load plus exactly one background load
    finally:
        encoder.close()


def test_a_background_load_counts_as_activity_so_the_reaper_does_not_drop_it_at_once(claims_db) -> None:
    encoder = _encoder(idle_s=30.0)
    try:
        assert encoder.warm() is True
        _wait_loaded(encoder)
        assert encoder._last_query > 0 and time.monotonic() - encoder._last_query < 5
    finally:
        encoder.close()


def test_the_model_is_not_reported_loaded_while_its_child_is_still_starting() -> None:
    slow = ("import time; time.sleep(1.5)\n" + FAKE_CHILD)
    encoder = WorkerEncoder(4, "cpu", idle_s=0.0, command=[sys.executable, "-c", slow])
    try:
        assert encoder.warm() is True
        time.sleep(0.4)
        assert not encoder.loaded  # a query now must still be answered cold, not queue behind the load
        _wait_loaded(encoder)
    finally:
        encoder.close()
