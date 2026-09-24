"""In-process stdlib transport (ruling R6) against LOCAL servers only (never the provider).

Covers review F-15/F-10 and review-A r3_transport.py: no subprocess worker (no cwd
module shadowing), no httpx import, proxy environment ignored, redirects refused,
128 KiB response cap, hard deadline (including slow-drip bodies), HTTP
401/403/429/529 outcomes, bounded batch retries, malformed answers and the served
model recorded as served; keep-alive reuse and reconnection; TLS verification with
the system trust store, the one-time certifi retry and the cached context.
"""
from __future__ import annotations

import http.client
import http.server
import json
import math
import os
import socket
import ssl
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from memorymaster.decisions import transport as tr
from memorymaster.decisions.credentials import ApiKey
from memorymaster.decisions.questions import AnswerSchema
from memorymaster.decisions.transport import (
    HttpTransport,
    HttpxTransport,
    TransportResult,
    cost_usd,
    validate_response,
)

REPO = Path(__file__).resolve().parents[1]

KEY = ApiKey("ts-test-" + "K" * 24)  # synthetic
EXPECTED = {
    "q.noul": AnswerSchema("noul"),
    "q.choice": AnswerSchema("choice", options=("a", "b", "unknown")),
    "q.score": AnswerSchema("score", levels=(1, 2, 3)),
}


def good_body(model: str = "jev-1.13.0") -> dict:
    return {
        "model": model,
        "answers": {
            "q.noul": {"type": "noul", "noul": 0.8},
            "q.choice": {"type": "choice", "choice": "b", "confidence": 0.9,
                         "probabilities": {"a": 0.05, "b": 0.9, "unknown": 0.05}},
            # Live form: probabilities keyed by 0-based index into the criteria sent.
            "q.score": {"type": "score", "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7}},
        },
        "usage": {"input_tokens": 1000, "output_tokens": 12},
    }


class _QuietServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # client aborts are expected here
        pass


class Server:
    """Scripted local HTTP server; each entry is a callable(handler) -> None."""

    def __init__(self, tls_context=None, host: str = "127.0.0.1"):
        self.script: list = []
        self.host = host
        self.requests: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body,
                                       "client": self.client_address})
                action = outer.script.pop(0) if outer.script else respond_json(good_body())
                action(self)

            def do_GET(self):  # noqa: N802
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": b""})
                respond_json({"redirected": True})(self)

            def log_message(self, *args):
                pass

        self.httpd = _QuietServer(("127.0.0.1", 0), Handler)
        if tls_context is not None:
            self.httpd.socket = tls_context.wrap_socket(self.httpd.socket, server_side=True)
        self.scheme = "https" if tls_context is not None else "http"
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.httpd.server_address[1]}/v1/systemone"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def respond_json(payload, status: int = 200, headers: dict | None = None, raw: bytes | None = None):
    def action(handler):
        body = raw if raw is not None else json.dumps(payload).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(body)
    return action


def respond_sleep(seconds: float):
    def action(handler):
        time.sleep(seconds)
        respond_json(good_body())(handler)
    return action


def respond_drip(seconds_per_byte: float, total: int = 50):
    def action(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        try:
            for _ in range(total):
                handler.wfile.write(b"1\r\n \r\n")
                handler.wfile.flush()
                time.sleep(seconds_per_byte)
            handler.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass
    return action


def respond_chunked_large(size: int):
    def action(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        chunk = b"x" * 8192
        sent = 0
        try:
            while sent < size:
                handler.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                sent += len(chunk)
            handler.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass
    return action


@pytest.fixture()
def server():
    srv = Server()
    yield srv
    srv.close()


@pytest.fixture()
def transport(server):
    t = HttpTransport(KEY, endpoint=server.url)
    yield t
    t.close()


def send(t: HttpTransport, timeout_s: float = 3.0, max_retries: int = 0) -> TransportResult:
    return t.send({"model": tr.MODEL, "state": {"x": 1}, "questions": {}}, expected=EXPECTED,
                  timeout_s=timeout_s, max_retries=max_retries)


LIVE_FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "jev_live_responses_20260923.json").read_text(encoding="utf-8"))
LIVE_EXPECTED = {
    "rel": AnswerSchema("score", levels=(1, 2, 3, 4)),
    "ch": AnswerSchema("choice", options=("legacy", "hybrid", "none")),
    "n": AnswerSchema("noul"),
}


@pytest.mark.parametrize("name", ["string_criteria", "object_criteria"])
def test_recorded_live_answers_parse_through_the_transport(server, transport, name):
    """Real jev-1.13.0 bodies: Score probabilities are keyed by 0-based index, not by level."""
    body = LIVE_FIXTURES[name]
    expected = {wire_id: LIVE_EXPECTED[wire_id] for wire_id in body["answers"]}
    server.script.append(respond_json(body))
    result = transport.send({"model": tr.MODEL, "state": {}, "questions": {}}, expected=expected, timeout_s=3.0)
    assert result.outcome == "ok", result.error_class
    assert result.model_served == "jev-1.13.0"
    assert (result.tokens_in, result.tokens_out) == (body["usage"]["input_tokens"], body["usage"]["output_tokens"])
    raw = body["answers"]["rel"]
    rel = result.answers["rel"]
    assert rel.primitive == "score"
    # index i -> spec level i+1, exactly as the legend reports it
    assert rel.probabilities == {str(i + 1): raw["probabilities"][str(i)] for i in range(4)}
    assert rel.label == "4"
    assert rel.confidence == pytest.approx(raw["confidence"])
    assert rel.value == pytest.approx(sum(raw["probabilities"][str(i)] * i / 3 for i in range(4)))
    if name == "string_criteria":
        assert rel.value == pytest.approx(0.97)
        choice = result.answers["ch"]
        assert choice.value == "hybrid" and choice.label == "hybrid"
        assert choice.probabilities == {"legacy": 0.34, "hybrid": 0.6, "none": 0.06}
        assert choice.confidence == pytest.approx(0.39)
        assert result.answers["n"].value == pytest.approx(0.96)
    else:
        assert rel.value == pytest.approx(0.04 / 3 + 0.36 * 2 / 3 + 0.59)


@pytest.mark.parametrize("name", ["string_criteria", "object_criteria"])
def test_recorded_live_answers_pass_validate_response(name):
    body = LIVE_FIXTURES[name]
    validated = validate_response(body, {wire_id: LIVE_EXPECTED[wire_id] for wire_id in body["answers"]})
    assert set(validated.answers) == set(body["answers"])
    assert validated.usage_reported


def test_constants_are_pinned():
    assert tr.ENDPOINT == "https://api.typesafe.ai/v1/systemone"
    assert tr.MODEL == "jev-1.13.0"
    assert tr.MAX_RESPONSE_BYTES == 128 * 1024


def test_httpx_is_not_a_core_dependency():
    """R6: the transport is the standard library; httpx stays only where Qdrant needs it."""
    tomllib = pytest.importorskip("tomllib")
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    core = [requirement.replace(" ", "") for requirement in project["project"]["dependencies"]]
    assert not [requirement for requirement in core if requirement.lower().startswith("httpx")]
    qdrant = [requirement.replace(" ", "") for requirement in project["project"]["optional-dependencies"]["qdrant"]]
    assert "httpx>=0.27" in qdrant


def test_httpx_transport_is_an_alias_and_versions_name_the_stdlib():
    assert HttpxTransport is HttpTransport
    assert tr.transport_version() == f"stdlib-http.client/py{sys.version_info[0]}.{sys.version_info[1]}"
    assert "httpx" not in tr.TRANSPORT_VERSION and not hasattr(tr, "httpx_version")


_COLD_CLIENT = textwrap.dedent(
    """
    import http.server, json, sys, threading, time
    started = time.perf_counter()
    sys.path.insert(0, {repo!r})
    from memorymaster.decisions import transport
    from memorymaster.decisions.credentials import ApiKey
    from memorymaster.decisions.questions import AnswerSchema
    imported = time.perf_counter()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({{"model": "jev-1.13.0", "answers": {{"q": {{"type": "noul", "noul": 0.9}}}},
                               "usage": {{"input_tokens": 10, "output_tokens": 0}}}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/v1/systemone" % server.server_address[1]
    sent = time.perf_counter()
    result = transport.HttpTransport(ApiKey("ts-test-" + "C" * 24), endpoint=url).send(
        {{"model": transport.MODEL, "state": {{}}, "questions": {{}}}},
        expected={{"q": AnswerSchema("noul")}}, timeout_s=5.0)
    done = time.perf_counter()
    print(json.dumps({{"outcome": result.outcome, "httpx": "httpx" in sys.modules,
                      "import_ms": round((imported - started) * 1000, 1),
                      "request_ms": round((done - sent) * 1000, 1),
                      "import_to_answer_ms": round((imported - started + done - sent) * 1000, 1)}}))
    server.shutdown()
    """
)


def test_transport_module_never_imports_httpx_in_a_fresh_interpreter(tmp_path):
    """R6: importing and using the transport must not load httpx (cold hook cost)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MEMORYMASTER_", "TYPESAFE_"))}
    out = subprocess.run([sys.executable, "-I", "-c", _COLD_CLIENT.format(repo=str(REPO))], cwd=str(tmp_path),
                         env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout.strip().splitlines()[-1])
    assert report["outcome"] == "ok"
    assert report["httpx"] is False


def test_endpoint_must_be_pinned_or_loopback():
    HttpTransport(KEY)  # pinned default
    HttpTransport(KEY, endpoint="http://127.0.0.1:9/v1/systemone")
    for bad in ("https://evil.example/v1/systemone", "http://api.typesafe.ai/v1/systemone",
                "https://api.typesafe.ai.evil.example/v1/systemone"):
        with pytest.raises(ValueError):
            HttpTransport(KEY, endpoint=bad)


def test_ok_answers_are_validated_and_usage_costed(server, transport):
    result = send(transport)
    assert result.outcome == "ok" and result.status == 200
    assert result.attempt_count == 1
    assert result.model_served == "jev-1.13.0"
    assert result.tokens_in == 1000 and result.tokens_out == 12
    assert result.cost_usd == pytest.approx(1000 * 0.042e-6)
    assert result.answers["q.noul"].value == pytest.approx(0.8)
    assert result.answers["q.choice"].value == "b"
    assert result.answers["q.choice"].confidence == pytest.approx(0.9)
    assert result.answers["q.score"].label == "3"
    assert result.answers["q.score"].probabilities == {"1": 0.1, "2": 0.2, "3": 0.7}
    assert result.answers["q.score"].value == pytest.approx(0.1 * 0 + 0.2 * 0.5 + 0.7 * 1.0)
    sent = server.requests[0]
    assert sent["headers"]["Authorization"] == f"Bearer {KEY.reveal()}"
    assert json.loads(sent["body"])["model"] == "jev-1.13.0"


def test_model_is_recorded_as_served(server, transport):
    server.script.append(respond_json(good_body(model="jev-1.13.0-rc2")))
    result = send(transport)
    assert result.outcome == "ok"
    assert result.model_served == "jev-1.13.0-rc2"


def test_no_subprocess_is_spawned(server, transport, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("transport must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    assert send(transport).outcome == "ok"


def test_proxy_environment_is_ignored(server, monkeypatch):
    seen: list[bytes] = []
    proxy = socket.socket()
    proxy.bind(("127.0.0.1", 0))
    proxy.listen(5)
    proxy.settimeout(3)

    def accept():
        try:
            conn, _ = proxy.accept()
            seen.append(conn.recv(4096))
            conn.close()
        except OSError:
            pass

    threading.Thread(target=accept, daemon=True).start()
    proxy_url = f"http://127.0.0.1:{proxy.getsockname()[1]}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, proxy_url)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    t = HttpTransport(KEY, endpoint=server.url)
    try:
        result = send(t)
    finally:
        t.close()
        proxy.close()
    assert result.outcome == "ok"
    assert seen == []
    assert len(server.requests) == 1


def test_proxy_capture_control_env_honouring_client_would_be_intercepted(server, monkeypatch):
    """Control for the test above: urllib's default opener DOES follow the proxy env."""
    import urllib.error
    import urllib.request

    seen: list[bytes] = []
    proxy = socket.socket()
    proxy.bind(("127.0.0.1", 0))
    proxy.listen(5)
    proxy.settimeout(3)

    def accept():
        try:
            conn, _ = proxy.accept()
            seen.append(conn.recv(4096))
            conn.close()
        except OSError:
            pass

    threading.Thread(target=accept, daemon=True).start()
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.getsockname()[1]}")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    naive = urllib.request.build_opener()
    with pytest.raises((urllib.error.URLError, OSError, http.client.HTTPException)):
        naive.open(urllib.request.Request(server.url, data=b"{}", method="POST"), timeout=2)
    proxy.close()
    assert seen and b"/v1/systemone" in seen[0]
    assert server.requests == []


def test_redirects_are_refused(server, transport):
    target = server.url.replace("/v1/systemone", "/elsewhere")
    server.script.append(respond_json({}, status=302, headers={"Location": target}))
    result = send(transport)
    assert result.outcome == "redirect_refused"
    assert result.answers is None
    assert [r["path"] for r in server.requests] == ["/v1/systemone"]


def test_response_over_128_kib_is_rejected_by_length(server, transport):
    server.script.append(respond_json(None, raw=b"{" + b" " * (129 * 1024) + b"}"))
    result = send(transport)
    assert result.outcome == "too_large" and result.answers is None


def test_response_over_128_kib_is_rejected_when_chunked(server, transport):
    server.script.append(respond_chunked_large(256 * 1024))
    result = send(transport)
    assert result.outcome == "too_large"


def test_timeout_returns_within_budget(server, transport):
    server.script.append(respond_sleep(2.0))
    started = time.monotonic()
    result = send(transport, timeout_s=0.3)
    elapsed = time.monotonic() - started
    assert result.outcome == "timeout"
    assert elapsed < 1.5
    assert result.answers is None


def test_slow_drip_body_cannot_extend_the_deadline(server, transport):
    server.script.append(respond_drip(0.05, total=60))
    started = time.monotonic()
    result = send(transport, timeout_s=0.5)
    assert result.outcome == "timeout"
    assert time.monotonic() - started < 1.5


@pytest.mark.parametrize("status,outcome", [(401, "http_401"), (403, "http_403"), (422, "http_422"),
                                            (429, "http_429"), (529, "http_529"), (500, "http_5xx"),
                                            (503, "http_5xx"), (404, "http_4xx")])
def test_http_errors_map_to_outcomes(server, transport, status, outcome):
    server.script.append(respond_json({"error": "x"}, status=status))
    result = send(transport)
    assert result.outcome == outcome
    assert result.status == status
    assert result.attempt_count == 1
    assert result.answers is None
    assert result.cost_usd == 0.0


def test_hook_mode_never_retries_429(server, transport):
    server.script.extend([respond_json({}, status=429), respond_json(good_body())])
    result = send(transport, max_retries=0)
    assert result.outcome == "http_429" and result.attempt_count == 1
    assert len(server.requests) == 1


def test_batch_mode_backs_off_on_429_and_529(server):
    sleeps: list[float] = []
    t = HttpTransport(KEY, endpoint=server.url, sleep=sleeps.append)
    server.script.extend([respond_json({}, status=429), respond_json({}, status=529), respond_json(good_body())])
    try:
        result = send(t, timeout_s=5.0, max_retries=3)
    finally:
        t.close()
    assert result.outcome == "ok"
    assert result.attempt_count == 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0] > 0


def test_batch_retries_are_capped_at_three(server):
    t = HttpTransport(KEY, endpoint=server.url, sleep=lambda s: None)
    server.script.extend([respond_json({}, status=429)] * 6)
    try:
        result = send(t, timeout_s=5.0, max_retries=3)
    finally:
        t.close()
    assert result.outcome == "http_429"
    assert result.attempt_count == 4
    assert len(server.requests) == 4


def test_retry_after_beyond_deadline_stops_retrying(server):
    sleeps: list[float] = []
    t = HttpTransport(KEY, endpoint=server.url, sleep=sleeps.append)
    server.script.extend([respond_json({}, status=429, headers={"Retry-After": "30"}), respond_json(good_body())])
    try:
        result = send(t, timeout_s=1.0, max_retries=3)
    finally:
        t.close()
    assert result.outcome == "http_429" and sleeps == []


def test_non_json_body_is_malformed(server, transport):
    server.script.append(respond_json(None, raw=b"<html>oops</html>"))
    assert send(transport).outcome == "malformed"


def test_connection_refused_is_network_error_without_key():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    t = HttpTransport(KEY, endpoint=f"http://127.0.0.1:{port}/v1/systemone")
    try:
        result = send(t, timeout_s=1.0)
    finally:
        t.close()
    assert result.outcome in {"network_error", "timeout"}
    assert KEY.reveal() not in repr(result)
    assert result.error_class and KEY.reveal() not in result.error_class


def test_request_over_size_cap_is_not_sent(server, transport):
    huge = {"model": tr.MODEL, "state": {"x": "y" * (tr.MAX_REQUEST_BYTES + 10)}, "questions": {}}
    result = transport.send(huge, expected=EXPECTED, timeout_s=1.0)
    assert result.outcome == "request_too_large" and result.attempt_count == 0
    assert server.requests == []


def test_missing_ssl_is_transport_unavailable(monkeypatch):
    monkeypatch.setattr(tr, "_import_ssl", lambda: None)
    t = HttpTransport(KEY)  # the pinned https endpoint: never reached without TLS
    result = send(t)
    assert result.outcome == "transport_unavailable" and result.attempt_count == 0


def test_error_class_is_the_exception_type_name_only(monkeypatch, server, transport):
    class Leaky(ConnectionResetError):
        pass

    def boom(*args, **kwargs):
        raise Leaky(f"reset while sending {KEY.reveal()}")

    monkeypatch.setattr(http.client.HTTPConnection, "request", boom)
    result = send(transport)
    assert result.outcome == "network_error" and result.error_class == "Leaky"
    assert KEY.reveal() not in repr(result)


# ------------------------------------------------------------ keep-alive -------

def test_sequential_requests_reuse_one_keep_alive_connection(server, transport):
    assert [send(transport).outcome for _ in range(3)] == ["ok"] * 3
    assert len({r["client"] for r in server.requests}) == 1


def test_concurrent_requests_never_share_a_connection(server):
    t = HttpTransport(KEY, endpoint=server.url)
    server.script.extend([respond_sleep(0.2)] * 4)
    results: list[TransportResult] = []
    lock = threading.Lock()

    def worker():
        outcome = send(t, timeout_s=5.0)
        with lock:
            results.append(outcome)

    try:
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert [r.outcome for r in results] == ["ok"] * 4
        assert len({r["client"] for r in server.requests}) == 4  # one connection per in-flight request
        before = len(server.requests)
        assert [send(t).outcome for _ in range(4)] == ["ok"] * 4  # then the idle ones are reused
        assert {r["client"] for r in server.requests[before:]} <= {r["client"] for r in server.requests[:before]}
    finally:
        t.close()


def test_a_failed_connection_is_replaced_by_a_new_one(server, transport):
    def drop(handler):
        handler.close_connection = True
        handler.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 999\r\n\r\n{")  # truncated body

    assert send(transport).outcome == "ok"
    server.script.append(drop)
    failed = send(transport)
    assert failed.outcome == "network_error" and failed.error_class == "IncompleteRead"
    assert send(transport).outcome == "ok"
    clients = [r["client"] for r in server.requests]
    assert clients[0] == clients[1] and clients[2] != clients[1]


def test_a_connection_the_server_closed_while_idle_is_not_reused(server, transport):
    def respond_then_close(handler):
        respond_json(good_body())(handler)
        handler.close_connection = True  # no Connection: close header: the client only sees EOF later

    server.script.append(respond_then_close)
    assert send(transport).outcome == "ok"
    time.sleep(0.2)
    second = send(transport)
    assert second.outcome == "ok", second.error_class
    assert len(server.requests) == 2 and server.requests[0]["client"] != server.requests[1]["client"]



def test_an_idle_connection_older_than_the_expiry_is_not_reused(server):
    """A long-lived engine (MCP server, steward) must not reuse a connection a NAT or
    firewall may have dropped without a FIN: the first request would wait out its deadline."""
    clock = {"t": 1000.0}
    t = HttpTransport(KEY, endpoint=server.url, clock=lambda: clock["t"])
    try:
        assert send(t).outcome == "ok"
        clock["t"] += tr.IDLE_EXPIRY_S - 1  # still fresh: reused
        assert send(t).outcome == "ok"
        clock["t"] += tr.IDLE_EXPIRY_S + 1  # idle past the expiry: discarded, a new connection
        assert send(t).outcome == "ok"
    finally:
        t.close()
    clients = [r["client"] for r in server.requests]
    assert len(clients) == 3 and clients[0] == clients[1] and clients[2] != clients[1]
    assert 5 <= tr.IDLE_EXPIRY_S <= 30

def test_engine_decisions_share_the_keep_alive_connection(server, tmp_path):
    """The engine calls every request from its own deadline thread: reuse must survive that."""
    from memorymaster.decisions.config import DecisionConfig
    from memorymaster.decisions.engine import DecisionEngine, JevChoice
    from memorymaster.decisions.questions import build_hints

    def answer(handler):
        payload = json.loads(server.requests[-1]["body"])
        body = {"model": "jev-1.13.0", "usage": {"input_tokens": 5, "output_tokens": 0},
                "answers": {wire: {"type": "noul", "noul": 0.1} for wire in payload["questions"]}}
        respond_json(body)(handler)

    server.script.extend([answer, answer])
    config = DecisionConfig.from_env({"MEMORYMASTER_JEV_MODE": "live",
                                      "MEMORYMASTER_DECISIONS_DB": str(tmp_path / "decisions.db"),
                                      "MEMORYMASTER_JEV_HOOK_DEADLINE_MS": "10000"})  # not a deadline test
    engine = DecisionEngine(config, key_lookup=lambda: KEY,
                            transport_factory=lambda key: HttpTransport(key, endpoint=server.url))
    state, bound = build_hints("we decided to use WAL")
    for _ in range(2):
        decision = engine.decide("hints", state=state, questions=bound, items=[], legacy_action=[],
                                 choose=lambda a: JevChoice(action=[]))
        assert decision.fallback_reason is None
    assert len(server.requests) == 2 and server.requests[0]["client"] == server.requests[1]["client"]


# ------------------------------------------------------------------- TLS -------

def _certificate(tmp_path: Path, name: str, hosts: tuple[str, ...]) -> tuple[Path, Path]:
    pytest.importorskip("cryptography")
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address(h)) if h[0].isdigit() else x509.DNSName(h) for h in hosts]),
                critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / f"{name}.pem", tmp_path / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


def _tls_server(tmp_path: Path, name: str, hosts: tuple[str, ...]) -> Server:
    cert, key = _certificate(tmp_path, name, hosts)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    srv = Server(tls_context=context)  # 127.0.0.1: "localhost" may try ::1 first (slow refusals on Windows)
    srv.cert = cert
    return srv


@pytest.fixture()
def tls_server(tmp_path):
    srv = _tls_server(tmp_path, "loopback", ("127.0.0.1",))
    yield srv
    srv.close()


@pytest.fixture()
def fresh_tls():
    tr._reset_tls_context()
    yield
    tr._reset_tls_context()


def _trusting(cert: Path):
    def factory(ssl_module):
        context = ssl_module.create_default_context(cafile=str(cert))
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        return context
    return factory


def test_tls_uses_the_system_store_first_and_caches_the_context_that_worked(tls_server, fresh_tls, monkeypatch):
    built: list[str] = []
    monkeypatch.setattr(tr, "_system_tls_context", lambda m: built.append("system") or _trusting(tls_server.cert)(m))
    monkeypatch.setattr(tr, "_certifi_tls_context", lambda m: built.append("certifi") or None)
    t = HttpTransport(KEY, endpoint=tls_server.url)
    try:
        assert send(t).outcome == "ok"
        t.close()  # a new connection must still reuse the cached context
        assert send(t).outcome == "ok"
    finally:
        t.close()
    assert built == ["system"]
    assert tls_server.requests[0]["headers"]["Authorization"] == f"Bearer {KEY.reveal()}"


def test_certificate_failure_retries_once_with_certifi_and_caches_it(tls_server, fresh_tls, monkeypatch):
    built: list[str] = []
    monkeypatch.setattr(tr, "_system_tls_context", lambda m: built.append("system") or m.create_default_context())
    monkeypatch.setattr(tr, "_certifi_tls_context",
                        lambda m: built.append("certifi") or _trusting(tls_server.cert)(m))
    t = HttpTransport(KEY, endpoint=tls_server.url)
    try:
        first = send(t)
        t.close()
        second = send(t)
    finally:
        t.close()
    assert (first.outcome, second.outcome) == ("ok", "ok"), (first.error_class, second.error_class)
    assert first.attempt_count == 1
    assert built == ["system", "certifi"]  # the certifi context is cached for the process


def test_untrusted_certificate_is_refused_without_leaking(tls_server, fresh_tls, monkeypatch):
    monkeypatch.setattr(tr, "_system_tls_context", lambda m: m.create_default_context())
    monkeypatch.setattr(tr, "_certifi_tls_context", lambda m: m.create_default_context())
    t = HttpTransport(KEY, endpoint=tls_server.url)
    try:
        result = send(t)
    finally:
        t.close()
    assert result.outcome == "network_error" and result.error_class == "SSLCertVerificationError"
    assert tls_server.requests == [] and KEY.reveal() not in repr(result)


def test_hostname_is_verified(tmp_path, fresh_tls, monkeypatch):
    srv = _tls_server(tmp_path, "other", ("other.example",))
    monkeypatch.setattr(tr, "_system_tls_context", _trusting(srv.cert))
    monkeypatch.setattr(tr, "_certifi_tls_context", lambda m: None)
    t = HttpTransport(KEY, endpoint=srv.url)
    try:
        result = send(t)
    finally:
        t.close()
        srv.close()
    assert result.outcome == "network_error" and result.error_class == "SSLCertVerificationError"
    assert srv.requests == []


def test_default_tls_context_verifies_against_the_system_store():
    context = tr._system_tls_context(ssl)
    assert context.check_hostname is True and context.verify_mode == ssl.CERT_REQUIRED


# ------------------------------------------------------------ validation -------

def _bad(mutate) -> str | None:
    body = good_body()
    mutate(body)
    try:
        validate_response(body, EXPECTED)
    except tr.MalformedResponse as exc:
        return exc.reason
    return None


@pytest.mark.parametrize("name,mutate", [
    ("model_missing", lambda b: b.pop("model")),
    ("model_empty", lambda b: b.__setitem__("model", "")),
    ("answers_missing", lambda b: b.pop("answers")),
    ("question_unanswered", lambda b: b["answers"].pop("q.noul")),
    ("extra_answer", lambda b: b["answers"].__setitem__("q.other", {"type": "noul", "noul": 0.5})),
    ("wrong_type", lambda b: b["answers"]["q.noul"].__setitem__("type", "choice")),
    ("noul_above_one", lambda b: b["answers"]["q.noul"].__setitem__("noul", 1.2)),
    ("noul_negative", lambda b: b["answers"]["q.noul"].__setitem__("noul", -0.1)),
    ("noul_nan", lambda b: b["answers"]["q.noul"].__setitem__("noul", math.nan)),
    ("noul_bool", lambda b: b["answers"]["q.noul"].__setitem__("noul", True)),
    ("noul_string", lambda b: b["answers"]["q.noul"].__setitem__("noul", "0.5")),
    ("choice_sum", lambda b: b["answers"]["q.choice"]["probabilities"].__setitem__("a", 0.5)),
    ("choice_unknown_option", lambda b: b["answers"]["q.choice"].__setitem__("choice", "zzz")),
    ("choice_missing_option", lambda b: b["answers"]["q.choice"]["probabilities"].pop("unknown")),
    ("choice_confidence_inf", lambda b: b["answers"]["q.choice"].__setitem__("confidence", math.inf)),
    ("score_sum", lambda b: b["answers"]["q.score"]["probabilities"].__setitem__("2", 0.2)),
    ("score_level_set", lambda b: b["answers"]["q.score"].__setitem__("probabilities", {"0": 0.5, "1": 0.5})),
    ("score_keyed_by_level", lambda b: b["answers"]["q.score"].__setitem__(
        "probabilities", {"1": 0.1, "2": 0.2, "3": 0.7})),
    ("score_prob_range", lambda b: b["answers"]["q.score"].__setitem__(
        "probabilities", {"0": -0.2, "1": 0.2, "2": 1.0})),
    ("score_legend_mismatch", lambda b: b["answers"]["q.score"].__setitem__(
        "legend", {"1": "low", "2": "mid", "3": "high"})),
    ("usage_negative", lambda b: b["usage"].__setitem__("input_tokens", -1)),
    ("usage_float", lambda b: b["usage"].__setitem__("output_tokens", 1.5)),
])
def test_malformed_answers_are_rejected(name, mutate):
    assert _bad(mutate) is not None, name


def test_sum_tolerance_is_at_least_one_hundredth():
    def within(b):
        b["answers"]["q.choice"]["probabilities"] = {"a": 0.05, "b": 0.9, "unknown": 0.059}

    def outside(b):
        b["answers"]["q.choice"]["probabilities"] = {"a": 0.05, "b": 0.9, "unknown": 0.02}

    assert _bad(within) is None
    assert _bad(outside) == "choice_sum"


def test_sum_tolerance_allows_two_decimal_rounding_per_option():
    """Live values are rounded to 2 decimals: each option may be off by 0.005."""
    def three_options(b):  # tolerance max(0.01, 0.015): 0.986 is rounding, 0.97 is not
        b["answers"]["q.choice"]["probabilities"] = {"a": 0.04, "b": 0.9, "unknown": 0.046}

    def three_options_short(b):
        b["answers"]["q.choice"]["probabilities"] = {"a": 0.04, "b": 0.9, "unknown": 0.03}

    assert _bad(three_options) is None
    assert _bad(three_options_short) == "choice_sum"
    four_levels = {"0": 0.01, "1": 0.0, "2": 0.05, "3": 0.92}  # sums to 0.98; tolerance 0.02
    body = {"model": "jev-1.13.0", "answers": {"s": {"type": "score", "probabilities": four_levels}}}
    assert validate_response(body, {"s": AnswerSchema("score", levels=(1, 2, 3, 4))}).answers["s"].label == "4"


def test_two_hundred_option_choice_rounded_to_0_97_is_accepted():
    options = tuple(f"o{i}" for i in range(200))
    probabilities = {option: (0.01 if i < 97 else 0.0) for i, option in enumerate(options)}
    assert abs(sum(probabilities.values()) - 0.97) < 1e-9
    body = {"model": "jev-1.13.0",
            "answers": {"big": {"type": "choice", "choice": "o0", "probabilities": probabilities}}}
    parsed = validate_response(body, {"big": AnswerSchema("choice", options=options)}).answers["big"]
    assert parsed.value == "o0" and len(parsed.probabilities) == 200


def test_malformed_body_over_http_is_logged_as_malformed(server, transport):
    body = good_body()
    body["answers"]["q.noul"]["noul"] = 7
    server.script.append(respond_json(body))
    result = send(transport)
    assert result.outcome == "malformed"
    assert result.answers is None
    assert result.error_class.startswith("malformed:")
    assert result.model_served == "jev-1.13.0"


def test_nan_literal_from_server_is_malformed(server, transport):
    raw = json.dumps(good_body()).replace("0.8", "NaN").encode()
    server.script.append(respond_json(None, raw=raw))
    assert send(transport).outcome == "malformed"


def test_cost_table():
    assert cost_usd(1_000_000, 5_000) == pytest.approx(0.042)
    assert tr.PRICE_TABLE_VERSION


def test_a_lone_surrogate_reaching_the_transport_is_refused_not_guessed(server, transport):
    """The engine scrubs surrogates before redaction; one that still reaches the transport
    means redaction saw different text, so nothing is sent (review of 8c8d57e)."""
    result = transport.send({"model": tr.MODEL, "state": {"prompt": "ship it \ud83d now"}, "questions": {}},
                            expected=EXPECTED, timeout_s=3.0)
    assert result.outcome == "request_invalid" and server.requests == []
