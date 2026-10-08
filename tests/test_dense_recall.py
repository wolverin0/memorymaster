"""Dense prompt recall (EmbeddingGemma 2 service), measured 2026-10-06.

The lexical prompt hook scored nDCG@10 0.167 on 200 real prompts and injected
5.9 claims per prompt, 72% of its top 5 useless; the dense service scored 0.337
with the same scope. These tests pin the policy that produced that result:
dense candidates replace the lexical pool, only scores >= 0.68 are injected,
every candidate passes the same authorization as any other stream, nothing is
injected when nothing qualifies, and lexical recall runs when the service is down.
"""
from __future__ import annotations

import hashlib
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from memorymaster.core import lifecycle
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall import context_hook, dense_recall
from memorymaster.recall.dense_server import DenseIndex, DenseService, make_handler, read_confirmed_claims
from memorymaster.recall.qdrant_outbox import ENV_OUTBOX_DIR


@pytest.fixture()
def claims(tmp_path: Path, monkeypatch) -> dict:
    monkeypatch.setenv(ENV_OUTBOX_DIR, str(tmp_path / "outbox"))
    monkeypatch.setenv(dense_recall.LOG_ENV, str(tmp_path / "metrics" / "recall-dense.jsonl"))
    for name in ("QDRANT_URL", "MEMORYMASTER_SCOPE_DEFAULT"):
        monkeypatch.delenv(name, raising=False)
    svc = MemoryService(tmp_path / "dense.db", workspace_root=tmp_path)
    svc.init_db()
    ids = {}
    for key, scope, text in (
        ("recoil", "project:pubgclone", "weapon recoil spread pattern decided for the rifle"),
        ("netcode", "project:pubgclone", "netcode tick rate fixed at 60 Hz for the match server"),
        ("other", "project:memorymaster", "weapon recoil note that belongs to another project"),
    ):
        claim = svc.store.create_claim(text=text, citations=[CitationInput(source="test://dense")], scope=scope)
        lifecycle.transition_claim(svc.store, claim.id, "confirmed", reason="fixture", event_type="validator")
        ids[key] = claim.id
    cwd = tmp_path / "pubgclone"
    cwd.mkdir()
    return {"db": str(svc.store.db_path), "ids": ids, "hook": {"cwd": str(cwd), "session_id": "s"},
            "log": tmp_path / "metrics" / "recall-dense.jsonl"}


def _answer(*pairs: tuple[int, float], outside: tuple = ()) -> dense_recall.DenseAnswer:
    return dense_recall.DenseAnswer(tuple(pairs), 30.0, 1.0, 3, "google/embeddinggemma-2", tuple(outside))


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_dense_candidates_replace_the_lexical_pool_in_score_order(claims, monkeypatch) -> None:
    ids = claims["ids"]
    monkeypatch.setenv(dense_recall.ENABLE_ENV, "1")
    monkeypatch.setattr(dense_recall, "search", lambda query, scopes, **_: _answer(
        (ids["other"], 0.90), (ids["netcode"], 0.80), (ids["recoil"], 0.70)))
    text, rendered = context_hook.recall("how does weapon recoil spread work", db_path=claims["db"],
                                         return_ids=True, hook_data=claims["hook"])
    # The out-of-scope claim is authorized out even though the service ranked it first.
    assert rendered == [ids["netcode"], ids["recoil"]]
    event = _events(claims["log"])[-1]
    assert event["outcome"] == "dense"
    assert event["injected"] == 2 and event["filtered_out"] == 1
    assert event["injected_ids"] == rendered and event["chars"] == len(text)
    assert "weapon" not in json.dumps(event)  # the prompt text is never logged


def test_scores_below_the_threshold_are_not_injected(claims, monkeypatch) -> None:
    ids = claims["ids"]
    monkeypatch.setenv(dense_recall.ENABLE_ENV, "1")
    monkeypatch.setattr(dense_recall, "search", lambda query, scopes, **_: _answer(
        (ids["recoil"], 0.72), (ids["netcode"], 0.60)))
    _text, rendered = context_hook.recall("weapon recoil", db_path=claims["db"], return_ids=True,
                                          hook_data=claims["hook"])
    assert rendered == [ids["recoil"]]


def test_nothing_is_injected_when_no_candidate_qualifies(claims, monkeypatch) -> None:
    ids = claims["ids"]
    monkeypatch.setenv(dense_recall.ENABLE_ENV, "1")
    monkeypatch.setattr(dense_recall, "search", lambda query, scopes, **_: _answer((ids["recoil"], 0.50)))
    text, rendered = context_hook.recall("weapon recoil spread", db_path=claims["db"], return_ids=True,
                                         hook_data=claims["hook"])
    assert (text, rendered) == ("", [])
    assert _events(claims["log"])[-1]["outcome"] == "dense_empty"


def test_another_projects_claim_needs_a_very_high_score(claims, monkeypatch) -> None:
    # Operator 2026-10-06: other projects only on a very high score (>= 0.76 measured).
    ids = claims["ids"]
    monkeypatch.setenv(dense_recall.ENABLE_ENV, "1")
    monkeypatch.setattr(dense_recall, "search", lambda query, scopes, **_: _answer(
        (ids["recoil"], 0.70), outside=((ids["other"], 0.78),)))
    _text, rendered = context_hook.recall("weapon recoil", db_path=claims["db"], return_ids=True,
                                          hook_data=claims["hook"])
    assert rendered == [ids["other"], ids["recoil"]]
    assert _events(claims["log"])[-1]["injected_outside"] == 1
    monkeypatch.setattr(dense_recall, "search", lambda query, scopes, **_: _answer(
        (ids["recoil"], 0.70), outside=((ids["other"], 0.74),)))
    _text, rendered = context_hook.recall("weapon recoil", db_path=claims["db"], return_ids=True,
                                          hook_data=claims["hook"])
    assert rendered == [ids["recoil"]]


def test_another_projects_claim_still_passes_the_status_filter(claims, monkeypatch) -> None:
    svc = MemoryService(claims["db"], workspace_root=Path(claims["db"]).parent)
    draft = svc.store.create_claim(text="weapon recoil draft in another project",
                                   citations=[CitationInput(source="test://dense")], scope="project:memorymaster")
    monkeypatch.setenv(dense_recall.ENABLE_ENV, "1")
    monkeypatch.setattr(dense_recall, "search", lambda query, scopes, **_: _answer(outside=((draft.id, 0.95),)))
    _text, rendered = context_hook.recall("weapon recoil", db_path=claims["db"], return_ids=True,
                                          hook_data=claims["hook"])
    assert rendered == [] and _events(claims["log"])[-1]["filtered_out"] == 1


def test_the_service_receives_the_hooks_scope_allowlist(claims, monkeypatch) -> None:
    seen = {}
    monkeypatch.setenv(dense_recall.ENABLE_ENV, "1")

    def search(query, scopes, **_):
        seen["scopes"] = scopes
        return _answer()

    monkeypatch.setattr(dense_recall, "search", search)
    context_hook.recall("weapon recoil", db_path=claims["db"], hook_data=claims["hook"])
    assert "project:pubgclone" in seen["scopes"] and "project:memorymaster" not in seen["scopes"]


def test_an_unavailable_service_falls_back_to_lexical_recall(claims, monkeypatch) -> None:
    lexical = context_hook.recall("weapon recoil spread", db_path=claims["db"], return_ids=True,
                                  hook_data=claims["hook"])
    monkeypatch.setenv(dense_recall.ENABLE_ENV, "1")

    def down(query, scopes, **_):
        raise dense_recall.DenseUnavailable("URLError")

    monkeypatch.setattr(dense_recall, "search", down)
    fallback = context_hook.recall("weapon recoil spread", db_path=claims["db"], return_ids=True,
                                   hook_data=claims["hook"])
    assert fallback == lexical and lexical[1]
    assert _events(claims["log"])[-1]["outcome"] == "fallback:URLError"


def test_off_by_default_and_never_outside_the_prompt_hook(claims, monkeypatch) -> None:
    def must_not_run(*_args, **_kwargs):
        raise AssertionError("dense service called")

    monkeypatch.setattr(dense_recall, "search", must_not_run)
    assert context_hook.recall("weapon recoil", db_path=claims["db"], return_ids=True, hook_data=claims["hook"])[1]
    monkeypatch.setenv(dense_recall.ENABLE_ENV, "1")
    assert context_hook.recall("weapon recoil", db_path=claims["db"], return_ids=True)[1]
    assert not claims["log"].exists()


# ---- service side (no model: a deterministic bag-of-words encoder)

def _fake_encode(texts, kind):
    vectors = np.zeros((len(texts), 32), dtype=np.float32)
    for row, text in enumerate(texts):
        for word in text.lower().split():
            vectors[row, int(hashlib.md5(word.encode()).hexdigest(), 16) % 32] += 1.0
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.where(norms == 0, 1, norms)


class _CountingEncoder:
    def __init__(self) -> None:
        self.embedded = 0
        self.calls: list[int] = []

    def __call__(self, texts, kind):
        self.embedded += len(texts)
        self.calls.append(len(texts))
        return _fake_encode(texts, kind)


def test_a_burst_of_new_claims_is_embedded_in_small_chunks() -> None:
    # One call per burst held the encoder lock for the whole burst: on CPU, 100
    # newly confirmed claims would keep every prompt waiting ~9 s, past its timeout.
    encoder = _CountingEncoder()
    index = DenseIndex(encoder)
    index.sync([(i, "project:a", f"claim number {i}") for i in range(20)])
    assert encoder.calls == [8, 8, 4] and len(index.ids) == 20


def test_the_index_reembeds_only_new_or_changed_claims(tmp_path) -> None:
    encoder = _CountingEncoder()
    cache = tmp_path / "index.npz"
    index = DenseIndex(encoder, cache_path=cache)
    index.sync([(1, "project:a", "weapon recoil"), (2, "project:b", "tick rate")])
    assert encoder.embedded == 2
    assert index.sync([(1, "project:a", "weapon recoil"), (2, "project:b", "tick rate")]) == 0
    assert index.sync([(1, "project:a", "weapon recoil spread"), (3, "project:a", "new claim")]) == 2
    assert sorted(index.ids.tolist()) == [1, 3]
    reloaded = DenseIndex(_CountingEncoder(), cache_path=cache)
    assert sorted(reloaded.ids.tolist()) == [1, 3] and reloaded.sync([(1, "project:a", "weapon recoil spread"),
                                                                     (3, "project:a", "new claim")]) == 0


def test_index_search_honours_the_scope_allowlist(tmp_path) -> None:
    index = DenseIndex(_fake_encode)
    index.sync([(1, "project:a", "weapon recoil spread"), (2, "project:b", "weapon recoil spread")])
    query = _fake_encode(["weapon recoil"], "query")[0]
    assert [cid for cid, _ in index.search(query, scopes=["project:b"], k=5)] == [2]
    assert {cid for cid, _ in index.search(query, scopes=None, k=5)} == {1, 2}
    assert [cid for cid, _ in index.search(query, scopes=["project:b"], k=5, outside=True)] == [1]
    assert index.search(query, scopes=None, k=5, outside=True) == []


def test_the_client_talks_to_the_service_and_reports_a_dead_one(claims, monkeypatch) -> None:
    service = DenseService(claims["db"], _fake_encode, model="fake", dim=32, device="cpu")
    assert service.sync_once() == 3
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv(dense_recall.URL_ENV, f"http://127.0.0.1:{server.server_address[1]}")
        answer = dense_recall.search("weapon recoil", ["project:pubgclone"], outside_k=5)
        assert answer.indexed == 3 and answer.results[0][0] == claims["ids"]["recoil"]
        assert [cid for cid, _ in answer.outside] == [claims["ids"]["other"]]
        assert {cid for cid, _ in answer.results} <= {claims["ids"]["recoil"], claims["ids"]["netcode"]}
    finally:
        server.shutdown()
        server.server_close()
    monkeypatch.setenv(dense_recall.TIMEOUT_ENV, "0.5")
    with pytest.raises(dense_recall.DenseUnavailable):
        dense_recall.search("weapon recoil", None)


def test_only_confirmed_claims_are_indexed(claims) -> None:
    svc = MemoryService(claims["db"], workspace_root=Path(claims["db"]).parent)
    svc.store.create_claim(text="an unreviewed candidate", citations=[CitationInput(source="test://dense")],
                           scope="project:pubgclone")
    assert {cid for cid, _, _ in read_confirmed_claims(claims["db"])} == set(claims["ids"].values())


def test_the_report_summarizes_outcomes_latency_and_volume() -> None:
    events = [
        {"outcome": "dense", "service_ms": 40, "total_ms": 60, "injected": 4, "chars": 3000, "top_score": 0.75},
        {"outcome": "dense_empty", "service_ms": 38, "total_ms": 50, "injected": 0, "chars": 0, "top_score": 0.6},
        {"outcome": "fallback:URLError", "service_ms": 2, "total_ms": 3},
    ]
    report = dense_recall.summarize(events)
    assert report["recalls"] == 3 and report["fallback_rate"] == 0.333
    assert report["empty_rate"] == 0.5 and report["injected_mean"] == 2.0 and report["chars_mean"] == 1500


# ---- defects found by the independent verifier, 2026-10-06

def test_any_client_failure_becomes_a_fallback(monkeypatch) -> None:
    import http.server

    class Truncating(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b'{"results": [')  # then hang up: IncompleteRead

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Truncating)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv(dense_recall.URL_ENV, f"http://127.0.0.1:{server.server_address[1]}")
        with pytest.raises(dense_recall.DenseUnavailable):
            dense_recall.search("weapon recoil", None)
    finally:
        server.shutdown()
        server.server_close()
    monkeypatch.setenv(dense_recall.URL_ENV, "not a url")
    with pytest.raises(dense_recall.DenseUnavailable):
        dense_recall.search("weapon recoil", None)


def test_a_repeated_id_is_injected_once(claims, monkeypatch) -> None:
    ids = claims["ids"]
    monkeypatch.setenv(dense_recall.ENABLE_ENV, "1")
    monkeypatch.setattr(dense_recall, "search", lambda query, scopes, **_: _answer(
        (ids["recoil"], 0.80), (ids["recoil"], 0.79), (ids["netcode"], 0.70)))
    _text, rendered = context_hook.recall("weapon recoil", db_path=claims["db"], return_ids=True,
                                          hook_data=claims["hook"])
    assert rendered == [ids["recoil"], ids["netcode"]]


def test_the_service_rejects_a_non_object_body_and_an_empty_scope_list_matches_nothing(claims) -> None:
    import urllib.error
    import urllib.request

    service = DenseService(claims["db"], _fake_encode, model="fake", dim=32, device="cpu")
    service.sync_once()
    assert service.search("weapon recoil", [], 5)["results"] == []
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/search", data=b"[1, 2]",
                                         headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()


def test_a_cache_from_another_model_is_not_reused(tmp_path) -> None:
    cache = tmp_path / "index.npz"
    DenseIndex(_fake_encode, cache_path=cache, model_tag="model-a:768").sync([(1, "project:a", "weapon recoil")])
    encoder = _CountingEncoder()
    index = DenseIndex(encoder, cache_path=cache, model_tag="model-b:768")
    assert index.sync([(1, "project:a", "weapon recoil")]) == 1


def test_the_report_skips_lines_without_a_timestamp(tmp_path) -> None:
    log = tmp_path / "recall-dense.jsonl"
    log.write_text('{"outcome": "dense"}\n{"ts": "2026-10-06T23:00:00+00:00", "outcome": "dense"}\n', encoding="utf-8")
    assert len(dense_recall.read_events(log)) == 1
