"""Gemini embedding client against a stdlib loopback fake (no network, no ML).

Acceptance criteria S2 (a)-(h) of the Gemini dense plan; (f) per the operator's
2026-11-01 ruling: the egress is secrets-only (IPs, emails, paths pass; credentials
are redacted or blocked).
"""

from __future__ import annotations

import json
import logging
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import pytest

from memorymaster.core import provider_health
from memorymaster.core.usage_ledger import (
    EMBED_DOC_OPERATION,
    EMBED_QUERY_OPERATION,
    UsageLedger,
)
from memorymaster.decisions import egress
from memorymaster.decisions.egress import prepare_egress
from memorymaster.recall import gemini_embed
from memorymaster.recall.embeddings import GEMINI_MAX_BATCH, EncoderUnavailable
from memorymaster.recall.gemini_embed import (
    DOC_TEMPLATE,
    QUERY_TEMPLATE,
    EmbedUnavailable,
    GeminiEmbedder,
    chunk,
)

KEY = "KEYSENTINEL-9f8e7d6c5b4a3210-DoNotLeak"

Reply = tuple[int, dict[str, Any] | bytes | None, dict[str, str]]


def _vec(text: str, dims: int) -> list[float]:
    seed = sum(ord(c) for c in text) + 1
    return [float((seed * (i + 3)) % 17 + 1) for i in range(dims)]


class FakeGemini:
    """Loopback server. ``script`` may return a Reply for a request, or None = default ok."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.connections = 0
        self.script: Callable[[int, dict[str, Any]], Reply | None] | None = None
        self.delay = 0.0
        self.vector_len: int | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            disable_nagle_algorithm = True  # Windows delayed-ACK adds ~200 ms per reply otherwise

            def setup(self) -> None:
                outer.connections += 1
                super().setup()

            def log_message(self, *args: Any) -> None:  # silence
                pass

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.loads(raw)
                record = {
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "raw": raw,
                    "body": body,
                }
                outer.requests.append(record)
                if outer.delay:
                    import time

                    time.sleep(outer.delay)
                reply = outer.script(len(outer.requests), record) if outer.script else None
                if reply is None:
                    reply = (200, outer.ok_body(record), {})
                status, payload, headers = reply
                data = payload if isinstance(payload, bytes) else json.dumps(payload or {}).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def ok_body(self, record: dict[str, Any]) -> dict[str, Any]:
        body = record["body"]
        if record["path"].endswith(":batchEmbedContents"):
            items = body["requests"]
            return {
                "embeddings": [
                    {"values": _vec(i["content"]["parts"][0]["text"], self.vector_len or i["outputDimensionality"])}
                    for i in items
                ]
            }
        return {
            "embedding": {
                "values": _vec(
                    body["content"]["parts"][0]["text"], self.vector_len or body["outputDimensionality"]
                )
            }
        }

    def texts(self) -> list[str]:
        out: list[str] = []
        for r in self.requests:
            body = r["body"]
            for item in body.get("requests", [body]):
                out.append(item["content"]["parts"][0]["text"])
        return out

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def server():
    fake = FakeGemini()
    yield fake
    fake.stop()


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORYMASTER_EMBED_API_TIER", "paid")
    monkeypatch.setenv("MEMORYMASTER_USAGE_LEDGER_DB", str(tmp_path / "default-ledger.db"))
    for name in (
        "MEMORYMASTER_MAX_EMBED_QUERY_TOKENS_PER_DAY",
        "MEMORYMASTER_MAX_EMBED_DOC_TOKENS_PER_DAY",
        "MEMORYMASTER_EMBED_API_KEY",
        "MEMORYMASTER_EMBED_API_BASE_URL",
        "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    provider_health.reset()
    # The production 1.1 s query budget is a wall-clock bound; a loaded CI box would flake it.
    monkeypatch.setattr(gemini_embed, "QUERY_TIMEOUT_S", 15.0)


@pytest.fixture
def make(server, tmp_path):
    made: list[GeminiEmbedder] = []
    sleeps: list[float] = []

    def factory(**kw: Any) -> GeminiEmbedder:
        kw.setdefault("api_key", KEY)
        kw.setdefault("base_url", server.url)
        kw.setdefault("ledger_path", tmp_path / "ledger.db")
        kw.setdefault("sleep", sleeps.append)
        emb = GeminiEmbedder(**kw)
        emb.sleeps = sleeps  # type: ignore[attr-defined]
        made.append(emb)
        return emb

    yield factory
    for emb in made:
        emb.close()


def _norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


# ---- (a) chunking ----------------------------------------------------------------


def test_chunk_never_exceeds_limits_for_every_size() -> None:
    for n in range(0, 501):
        items = list(range(n))
        pieces = chunk(items)
        assert all(0 < len(p) <= GEMINI_MAX_BATCH for p in pieces)
        assert [x for p in pieces for x in p] == items
        # Even a caller asking for a huge size cannot exceed Google's 100.
        assert all(len(p) <= GEMINI_MAX_BATCH for p in chunk(items, 10_000))


def test_one_batch_constant_shared_with_the_embedding_provider() -> None:
    assert GEMINI_MAX_BATCH == 100
    assert not hasattr(gemini_embed, "MAX_BATCH") and not hasattr(gemini_embed, "DEFAULT_BATCH")


def test_documents_are_sent_via_batch_embed_in_requests_of_at_most_100(server, make) -> None:
    emb = make()
    vectors = emb.embed_documents([f"doc number {i}" for i in range(250)])
    sizes = [len(r["body"]["requests"]) for r in server.requests]
    assert sizes == [100, 100, 50]
    assert all(r["path"].endswith(":batchEmbedContents") for r in server.requests)
    assert len(vectors) == 250 and all(v is not None for v in vectors)
    assert emb.embed_documents([]) == []
    assert len(server.requests) == 3


def test_batch_size_is_clamped_to_100(server, make) -> None:
    emb = make(batch_size=5000)
    emb.embed_documents([f"d{i}" for i in range(150)])
    assert [len(r["body"]["requests"]) for r in server.requests] == [100, 50]


def test_default_dimension_is_768_everywhere(server, make) -> None:
    emb = make()
    assert emb.dims == 768 and emb.model_tag == "gemini-embedding-2:768:t1"
    q = emb.embed_query("hello")
    docs = emb.embed_documents(["a", "b"])
    assert len(q) == 768 and all(d is not None and len(d) == 768 for d in docs)
    for r in server.requests:
        items = r["body"].get("requests", [r["body"]])
        assert items and all(i["outputDimensionality"] == 768 for i in items)


def test_one_keep_alive_connection_serves_queries_and_batches(server, make) -> None:
    emb = make()
    emb.embed_query("q1")
    emb.embed_documents([f"d{i}" for i in range(150)])
    emb.embed_query("q2")
    assert len(server.requests) == 4 and server.connections == 1


# ---- (b) retries ---------------------------------------------------------------


def test_document_429_honours_retry_after(server, make) -> None:
    def script(n: int, _r: dict[str, Any]) -> Reply | None:
        return (429, {"error": {"status": "RESOURCE_EXHAUSTED"}}, {"Retry-After": "3"}) if n == 1 else None

    server.script = script
    emb = make()
    vectors = emb.embed_documents(["alpha"])
    assert vectors[0] is not None
    assert len(server.requests) == 2
    assert emb.sleeps == [3.0]  # type: ignore[attr-defined]


def test_document_429_retry_hint_in_body_without_header(server, make) -> None:
    body = {"error": {"message": "Quota exceeded. Please retry in 7.5s."}}
    server.script = lambda n, r: (429, body, {}) if n == 1 else None
    emb = make()
    assert emb.embed_documents(["alpha"])[0] is not None
    assert emb.sleeps == [7.5]  # type: ignore[attr-defined]


def test_open_breaker_skips_the_redaction_pass(server, make, monkeypatch) -> None:
    emb = make(clock=Clock())
    emb._open_until = emb._clock() + 60
    calls: list[str] = []
    monkeypatch.setattr(gemini_embed, "prepare_egress", lambda *a, **k: calls.append("x"))
    with pytest.raises(EmbedUnavailable) as err:
        emb.embed_documents(["a"] * 100)
    assert err.value.reason == "breaker" and calls == []


def test_document_5xx_backs_off_then_gives_up_and_opens_nothing_early(server, make) -> None:
    server.script = lambda n, r: (503, {}, {})
    emb = make()
    with pytest.raises(EmbedUnavailable) as err:
        emb.embed_documents(["alpha"])
    assert err.value.reason == "api"
    assert len(server.requests) == 4
    backoffs = emb.sleeps  # type: ignore[attr-defined]
    assert len(backoffs) == 3 and 1.0 <= backoffs[0] < 1.5 and 4.0 <= backoffs[2] < 5.5


def test_query_429_fails_fast_without_sleep(server, make) -> None:
    server.script = lambda n, r: (429, {}, {"Retry-After": "3"})
    emb = make()
    with pytest.raises(EmbedUnavailable) as err:
        emb.embed_query("what is the plan")
    assert err.value.reason == "rate"
    assert len(server.requests) == 1
    assert emb.sleeps == []  # type: ignore[attr-defined]


def test_query_retries_a_fast_5xx_once(server, make) -> None:
    server.script = lambda n, r: (500, {}, {}) if n == 1 else None
    emb = make()
    assert len(emb.embed_query("hello")) == 768
    assert len(server.requests) == 2


def test_connection_is_reused_across_requests(server, make) -> None:
    emb = make()
    for i in range(4):
        emb.embed_query(f"query {i}")
    assert server.connections == 1


# ---- (c) bisect on 400 ---------------------------------------------------------


def test_400_on_a_batch_bisects_and_good_items_still_embed(server, make) -> None:
    def script(_n: int, rec: dict[str, Any]) -> Reply | None:
        items = rec["body"]["requests"]
        if any("POISON" in i["content"]["parts"][0]["text"] for i in items):
            return 400, {"error": {"status": "INVALID_ARGUMENT"}}, {}
        return None

    server.script = script
    emb = make()
    texts = [f"good {i}" for i in range(7)] + ["POISON pill"]
    vectors = emb.embed_documents(texts)
    assert [v is None for v in vectors] == [False] * 7 + [True]
    assert all(abs(_norm(v) - 1.0) < 1e-6 for v in vectors[:7])  # type: ignore[arg-type]
    assert emb.sleeps == []  # type: ignore[attr-defined]  # 400 is never retried
    assert not emb.breaker_open


# ---- (d) spend cap, fail-closed ledger ---------------------------------------------


def test_cap_blocks_next_reserve_with_zero_requests(server, make, monkeypatch, tmp_path) -> None:
    # "alpha" templated is 25 chars -> 9 estimated tokens; cap of 10 fits one call only.
    monkeypatch.setenv("MEMORYMASTER_MAX_EMBED_DOC_TOKENS_PER_DAY", "10")
    emb = make()
    assert emb.embed_documents(["alpha"])[0] is not None
    seen = len(server.requests)
    with pytest.raises(EmbedUnavailable) as err:
        emb.embed_documents(["alpha"])
    assert err.value.reason == "capped"
    assert len(server.requests) == seen
    assert UsageLedger(tmp_path / "ledger.db").total(EMBED_DOC_OPERATION) == 9


def test_query_and_doc_partitions_are_independent(server, make, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_MAX_EMBED_DOC_TOKENS_PER_DAY", "1")
    emb = make()
    with pytest.raises(EmbedUnavailable):
        emb.embed_documents(["alpha"])
    assert len(emb.embed_query("alpha")) == 768


def test_zero_budget_is_not_unlimited(server, make, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_MAX_EMBED_QUERY_TOKENS_PER_DAY", "0")
    emb = make()
    with pytest.raises(EmbedUnavailable) as err:
        emb.embed_query("alpha")
    assert err.value.reason == "capped"
    assert server.requests == []


def test_unopenable_ledger_fails_closed(server, make, tmp_path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file")
    emb = make(ledger_path=blocker / "ledger.db")
    with pytest.raises(EmbedUnavailable) as err:
        emb.embed_query("alpha")
    assert err.value.reason == "ledger"
    with pytest.raises(EmbedUnavailable) as err2:
        emb.embed_documents(["alpha"])
    assert err2.value.reason == "ledger"
    assert server.requests == []


def test_successful_calls_are_recorded_in_the_ledger(server, make, tmp_path) -> None:
    emb = make()
    emb.embed_query("alpha")
    assert UsageLedger(tmp_path / "ledger.db").total(EMBED_QUERY_OPERATION) > 0


def test_local_query_rate_limit(server, make) -> None:
    emb = make(query_rate_per_min=2, clock=Clock())
    emb.embed_query("one")
    emb.embed_query("two")
    with pytest.raises(EmbedUnavailable) as err:
        emb.embed_query("three")
    assert err.value.reason == "rate"
    assert len(server.requests) == 2


# ---- (e) breaker ---------------------------------------------------------------


def test_breaker_opens_after_three_failures_and_closes_after_60s(server, make) -> None:
    clock = Clock()
    server.script = lambda n, r: (503, {}, {})
    emb = make(clock=clock)
    for _ in range(3):
        with pytest.raises(EmbedUnavailable) as err:
            emb.embed_query("hello")
        assert err.value.reason == "api"
    sent = len(server.requests)
    with pytest.raises(EmbedUnavailable) as err:
        emb.embed_query("hello")
    assert err.value.reason == "breaker"
    assert len(server.requests) == sent  # microsecond fail, nothing on the wire
    clock.t += 59
    with pytest.raises(EmbedUnavailable) as err:
        emb.embed_documents(["hello"])
    assert err.value.reason == "breaker"
    clock.t += 2
    server.script = None
    assert len(emb.embed_query("hello")) == 768
    assert not emb.breaker_open


def test_embed_unavailable_is_an_encoder_unavailable() -> None:
    err = EmbedUnavailable("capped")
    assert isinstance(err, EncoderUnavailable) and err.reason == "capped"


# ---- (f) egress: secrets-only, single choke point ------------------------------------

PASS_THROUGH = (
    r"host 10.0.0.5:8080 and 192.168.1.9 mail bob@example.com file C:\Users\bob\notes.txt "
    "also /home/alice/proj on nas.local phone +54 11 5555-1234"
)
CREDENTIALS = {
    "google_api_key": "AIza" + "A" * 35,
    "db_url": "postgres://svc:hunter2pw@db.internal:5432/app",
    "bearer": "Authorization: Bearer abc.def-123456",
    "assignment": "password=hunter2xyz",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----",
}


def test_every_byte_sent_is_secrets_only_egress_output(server, make) -> None:
    emb = make()
    texts = [PASS_THROUGH, *CREDENTIALS.values(), "plain note about rollout"]
    emb.embed_documents(texts)
    emb.embed_query(PASS_THROUGH + " " + CREDENTIALS["assignment"])
    expected = [
        DOC_TEMPLATE.format(content=prepare_egress(t, max_chars=8000, secrets_only=True).text)
        for t in texts
    ] + [
        QUERY_TEMPLATE.format(
            content=prepare_egress(
                PASS_THROUGH + " " + CREDENTIALS["assignment"], max_chars=4000, secrets_only=True
            ).text
        )
    ]
    assert server.texts() == expected
    wire = b"".join(r["raw"] for r in server.requests).decode()
    for secret in ("hunter2", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "abc.def-123456", "PRIVATE KEY"):
        assert secret not in wire


def test_ip_email_path_phone_and_host_go_through_unchanged(server, make) -> None:
    emb = make()
    emb.embed_query(PASS_THROUGH)
    assert server.texts() == [QUERY_TEMPLATE.format(content=PASS_THROUGH)]


def test_a_blocked_text_produces_no_request(server, make, monkeypatch) -> None:
    monkeypatch.setattr(egress, "_surviving_credentials", lambda _t: ["jwt_token"])
    emb = make()
    with pytest.raises(EmbedUnavailable) as err:
        emb.embed_query("anything")
    assert err.value.reason == "blocked"
    assert emb.embed_documents(["a", "b"]) == [None, None]
    assert server.requests == []


def test_blocked_document_is_skipped_but_neighbours_embed(server, make, monkeypatch) -> None:
    real = egress._surviving_credentials
    monkeypatch.setattr(
        egress, "_surviving_credentials", lambda t: ["jwt_token"] if "BLOCKME" in t else real(t)
    )
    emb = make()
    vectors = emb.embed_documents(["fine one", "BLOCKME now", "fine two"])
    assert [v is None for v in vectors] == [False, True, False]
    assert len(server.texts()) == 2


def test_http_payloads_cannot_be_built_from_unprepared_text(make) -> None:
    emb = make()
    with pytest.raises(TypeError):
        emb._item("raw text")  # type: ignore[arg-type]


def test_truncation_caps_exposure(server, make) -> None:
    emb = make()
    emb.embed_query("word " * 5000)
    emb.embed_documents(["word " * 5000])
    assert len(server.texts()[0]) <= len(QUERY_TEMPLATE) + 4000
    assert len(server.texts()[1]) <= len(DOC_TEMPLATE) + 8000


# ---- (g) the key never leaks -----------------------------------------------------


def test_key_is_only_in_the_header_and_never_in_logs_urls_or_exceptions(
    server, make, caplog, monkeypatch
) -> None:
    monkeypatch.setattr(gemini_embed, "QUERY_TIMEOUT_S", 0.3)
    caplog.set_level(logging.DEBUG)
    emb = make(clock=__import__("time").monotonic)
    errors: list[BaseException] = []

    def attempt(fn: Callable[[], Any]) -> None:
        try:
            fn()
        except EmbedUnavailable as exc:
            errors.append(exc)

    for status in (401,):
        server.script = lambda n, r, s=status: (s, {"error": {"message": KEY}}, {})
        attempt(lambda: emb.embed_query("hello"))
    emb.auth_failed = False
    for reply in ((429, {"error": {"message": KEY}}, {"Retry-After": "1"}), (500, {}, {}), (200, b"<html>nope</html>", {})):
        server.script = lambda n, r, rep=reply: rep
        attempt(lambda: emb.embed_documents(["hello"]))
        emb._failures = 0
        emb._open_until = 0.0
    server.script = lambda n, r: (200, {"embedding": {"values": [1.0]}}, {})
    attempt(lambda: emb.embed_query("hello"))  # too-short vector
    server.script = None
    server.delay = 0.6
    emb._failures = 0
    attempt(lambda: emb.embed_query("hello"))  # timeout

    assert len(errors) >= 5
    assert server.requests and all(r["headers"]["x-goog-api-key"] == KEY for r in server.requests)
    assert all(KEY not in r["path"] for r in server.requests)
    assert all(KEY.encode() not in r["raw"] for r in server.requests)
    assert KEY not in caplog.text
    for exc in errors:
        assert KEY not in str(exc) and KEY not in repr(exc) and KEY not in repr(exc.__dict__)


def test_key_is_not_in_the_embedder_repr_or_str(make) -> None:
    emb = make()
    assert KEY not in repr(emb) and KEY not in str(emb)


def test_transport_failure_exception_chain_never_holds_the_key(server, make, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    emb = make()
    server.script = lambda n, r: (503, {"error": {"message": KEY}}, {})
    with pytest.raises(EmbedUnavailable) as err:
        emb.embed_documents(["hello"])
    chain = [err.value, err.value.__cause__, err.value.__context__]
    assert all(KEY not in f"{e!s}{e!r}" for e in chain if e is not None)
    assert KEY not in caplog.text


def test_401_latches_auth_failed(server, make) -> None:
    server.script = lambda n, r: (401, {}, {})
    emb = make()
    with pytest.raises(EmbedUnavailable) as err:
        emb.embed_query("hello")
    assert err.value.reason == "auth" and emb.auth_failed
    sent = len(server.requests)
    with pytest.raises(EmbedUnavailable) as err2:
        emb.embed_documents(["hello"])
    assert err2.value.reason == "auth" and len(server.requests) == sent


def test_missing_key_or_tier_attestation_refuses_to_start(monkeypatch, make) -> None:
    with pytest.raises(EmbedUnavailable) as err:
        GeminiEmbedder(api_key="", base_url="http://127.0.0.1:1")
    assert err.value.reason == "auth"
    monkeypatch.setenv("GEMINI_API_KEY", "other-key")  # never used as a fallback
    with pytest.raises(EmbedUnavailable):
        GeminiEmbedder(base_url="http://127.0.0.1:1")
    monkeypatch.delenv("MEMORYMASTER_EMBED_API_TIER")
    with pytest.raises(EmbedUnavailable) as err2:
        make()
    assert err2.value.reason == "auth"


def test_non_loopback_plain_http_base_url_is_refused() -> None:
    with pytest.raises(EmbedUnavailable):
        GeminiEmbedder(api_key=KEY, base_url="http://example.com")


# ---- (h) vectors ----------------------------------------------------------------


@pytest.mark.parametrize("dims", [256, 768])
def test_vectors_are_l2_normalized_at_the_requested_dimension(server, make, dims) -> None:
    emb = make(dims=dims)
    q = emb.embed_query("hello")
    docs = emb.embed_documents(["a doc", "another doc"])
    assert len(q) == dims and abs(_norm(q) - 1.0) < 1e-6
    for d in docs:
        assert d is not None and len(d) == dims and abs(_norm(d) - 1.0) < 1e-6
    for r in server.requests:
        items = r["body"].get("requests", [r["body"]])
        assert all(i["outputDimensionality"] == dims for i in items)
        assert all(i["model"] == "models/gemini-embedding-2" for i in items)
    assert emb.model_tag == f"gemini-embedding-2:{dims}:t1"


def test_longer_vector_is_truncated_and_renormalized_shorter_is_an_error(server, make) -> None:
    emb = make(dims=64)
    server.vector_len = 100
    v = emb.embed_query("hello")
    assert len(v) == 64 and abs(_norm(v) - 1.0) < 1e-6
    server.vector_len = 10
    with pytest.raises(EmbedUnavailable) as err:
        emb.embed_query("hello again")
    assert err.value.reason == "api"


def test_templates_are_applied(server, make) -> None:
    emb = make()
    emb.embed_query("find x")
    emb.embed_documents(["claim body"])
    assert server.texts() == ["task: search result | query: find x", "title: none | text: claim body"]
    assert server.requests[0]["path"] == "/v1beta/models/gemini-embedding-2:embedContent"
    assert server.requests[1]["path"] == "/v1beta/models/gemini-embedding-2:batchEmbedContents"
