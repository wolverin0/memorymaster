"""RED contracts for authenticated, verified Qdrant transport.

Every external call is replaced with a deterministic fake.  These tests are
intentionally added before the R1.5 transport implementation.
"""

from __future__ import annotations

import json
import importlib
import io
import logging
import sqlite3
import ssl
import sys
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

import certifi
import pytest

from memorymaster.recall import qdrant_backend, qdrant_recall_fallback, verbatim_store
from memorymaster.recall.qdrant_transport import QdrantTransportConfig
from memorymaster.surfaces import dashboard, setup_detect


QDRANT_KEY = "qdrant-test-secret-never-log-or-forward"
OPENAI_KEY = "openai-test-secret"


class _Response:
    def __init__(self, payload: dict[str, Any] | None = None, *, status: int = 200) -> None:
        self._payload = payload or {}
        self.status = status
        self.status_code = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _ObservedHttpxClient:
    def __init__(self, kwargs: dict[str, Any], *, failure_secret: str = "") -> None:
        self.constructor_kwargs = kwargs
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.failure_secret = failure_secret

    def _call(self, method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        self.calls.append((method, url, kwargs))
        if self.failure_secret and "qdrant.invalid" in url:
            raise RuntimeError(f"synthetic failure: {self.failure_secret}")
        if url.endswith("/api/embed"):
            dims = qdrant_backend.EMBEDDING_DIMS
            return _Response({"embeddings": [[0.0] * dims]})
        return _Response()

    def get(self, url: str, **kwargs: Any) -> _Response:
        return self._call("GET", url, kwargs)

    def put(self, url: str, **kwargs: Any) -> _Response:
        return self._call("PUT", url, kwargs)

    def post(self, url: str, **kwargs: Any) -> _Response:
        return self._call("POST", url, kwargs)

    def close(self) -> None:
        return None


def _install_httpx_clients(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failure_secret: str = "",
) -> list[_ObservedHttpxClient]:
    clients: list[_ObservedHttpxClient] = []

    def factory(**kwargs: Any) -> _ObservedHttpxClient:
        client = _ObservedHttpxClient(kwargs, failure_secret=failure_secret)
        clients.append(client)
        return client

    monkeypatch.setattr(qdrant_backend.httpx, "Client", factory)
    return clients


def _clients_for_url(
    clients: list[_ObservedHttpxClient],
    needle: str,
) -> list[_ObservedHttpxClient]:
    return [client for client in clients if any(needle in call[1] for call in client.calls)]


def _effective_headers(
    client: _ObservedHttpxClient,
    call: tuple[str, str, dict[str, Any]],
) -> dict[str, str]:
    headers = dict(client.constructor_kwargs.get("headers") or {})
    headers.update(call[2].get("headers") or {})
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _request_headers(request: Any) -> dict[str, str]:
    if isinstance(request, str):
        return {}
    return {key.lower(): value for key, value in request.header_items()}


def _request_url(request: Any) -> str:
    return request if isinstance(request, str) else request.full_url


def _assert_verified_context(context: Any) -> None:
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def _assert_custom_ca(verify: Any, ca_path: Path) -> None:
    if isinstance(verify, ssl.SSLContext):
        _assert_verified_context(verify)
        return
    assert Path(verify).resolve() == ca_path.resolve()


def _install_qdrant_open(monkeypatch: pytest.MonkeyPatch, callback: Any) -> None:
    def open_request(
        transport: QdrantTransportConfig,
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> Any:
        context = transport.ssl_context() if request.full_url.startswith("https://") else None
        return callback(request, timeout, context=context)

    monkeypatch.setattr(QdrantTransportConfig, "open", open_request)


def _install_fake_qdrant_module(
    monkeypatch: pytest.MonkeyPatch,
    factory: Any,
) -> None:
    module = ModuleType("qdrant_client")
    module.QdrantClient = factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "qdrant_client", module)


def _load_indexer_without_rewrapping_pytest_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    return importlib.import_module("scripts.index_claims_to_qdrant")


@pytest.fixture(autouse=True)
def _clean_qdrant_environment(monkeypatch: pytest.MonkeyPatch):
    for name in ("QDRANT_API_KEY", "QDRANT_CA_CERT", "MEMORYMASTER_QDRANT_URL"):
        monkeypatch.delenv(name, raising=False)
    qdrant_recall_fallback.reset_singletons_for_tests()
    yield
    qdrant_recall_fallback.reset_singletons_for_tests()


def test_transport_rejects_remote_http_and_limits_loopback_exception() -> None:
    transport = QdrantTransportConfig(api_key=QDRANT_KEY)

    for url in ("http://qdrant:6333", "http://192.0.2.10:6333"):
        with pytest.raises(ValueError, match="HTTPS"):
            transport.request(url, method="GET")

    assert transport.request("http://localhost:6333", method="GET").full_url.startswith("http://localhost")
    assert transport.request("http://127.0.0.1:6333", method="GET").full_url.startswith("http://127.0.0.1")

    transport_with_ca = QdrantTransportConfig(api_key=QDRANT_KEY, ca_cert=Path(certifi.where()))
    with pytest.raises(ValueError, match="HTTPS"):
        transport_with_ca.request("http://localhost:6333", method="GET")


def test_transport_opener_refuses_redirect_before_key_can_be_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: list[object] = []
    forwarded: list[urllib.request.Request] = []

    class _Opener:
        def open(self, request: urllib.request.Request, *, timeout: float) -> _Response:
            redirect_handler = next(
                handler for handler in handlers if isinstance(handler, urllib.request.HTTPRedirectHandler)
            )
            redirected = redirect_handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://attacker.invalid/collect",
            )
            if redirected is not None:
                forwarded.append(redirected)
            return _Response()

    def build_opener(*configured: object) -> _Opener:
        handlers.extend(configured)
        return _Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    transport = QdrantTransportConfig(api_key=QDRANT_KEY)
    request = transport.request("https://qdrant.invalid/collections", method="GET")

    transport.open(request, timeout=0.1)

    assert forwarded == []


@pytest.mark.parametrize("kind", ["missing", "directory", "invalid"])
def test_backend_rejects_invalid_ca_before_constructing_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    ca_path = tmp_path / "configured-ca.pem"
    if kind == "directory":
        ca_path.mkdir()
    elif kind == "invalid":
        ca_path.write_text("not a CA certificate", encoding="utf-8")
    monkeypatch.setenv("QDRANT_CA_CERT", str(ca_path))
    clients = _install_httpx_clients(monkeypatch)

    with pytest.raises((OSError, RuntimeError, ValueError), match="QDRANT_CA_CERT"):
        qdrant_backend.QdrantBackend(qdrant_url="https://qdrant.invalid")

    assert clients == []


def test_ca_resolution_error_is_fixed_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QDRANT_CA_CERT", "configured-ca.pem")

    def fail_resolve(self: Path, *, strict: bool = False) -> Path:
        raise RuntimeError(f"synthetic path failure: {QDRANT_KEY}")

    monkeypatch.setattr(Path, "resolve", fail_resolve)

    with pytest.raises(ValueError) as caught:
        QdrantTransportConfig.from_env()

    assert QDRANT_KEY not in str(caught.value)


def test_backend_uses_separate_clients_and_scopes_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QDRANT_API_KEY", QDRANT_KEY)
    clients = _install_httpx_clients(monkeypatch)
    backend = qdrant_backend.QdrantBackend(
        qdrant_url="https://qdrant.invalid",
        ollama_url="http://ollama.invalid",
    )

    backend.ensure_collection()
    assert backend._embed("safe text") is not None

    qdrant_clients = _clients_for_url(clients, "qdrant.invalid")
    ollama_clients = _clients_for_url(clients, "ollama.invalid")
    assert len(qdrant_clients) == len(ollama_clients) == 1
    assert qdrant_clients[0] is not ollama_clients[0]
    assert all(
        _effective_headers(qdrant_clients[0], call).get("api-key") == QDRANT_KEY for call in qdrant_clients[0].calls
    )
    assert QDRANT_KEY not in repr(ollama_clients[0].constructor_kwargs)
    assert QDRANT_KEY not in repr(ollama_clients[0].calls)


def test_backend_keeps_system_verification_without_custom_ca(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = _install_httpx_clients(monkeypatch)
    backend = qdrant_backend.QdrantBackend(
        qdrant_url="https://qdrant.invalid",
        ollama_url="http://ollama.invalid",
    )
    backend.ensure_collection()

    qdrant_client = _clients_for_url(clients, "qdrant.invalid")[0]
    assert qdrant_client.constructor_kwargs.get("verify", True) is not False


def test_backend_custom_ca_reaches_only_qdrant_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ca_path = Path(certifi.where())
    monkeypatch.setenv("QDRANT_CA_CERT", str(ca_path))
    clients = _install_httpx_clients(monkeypatch)
    backend = qdrant_backend.QdrantBackend(
        qdrant_url="https://qdrant.invalid",
        ollama_url="https://ollama.invalid",
    )
    backend.ensure_collection()
    backend._embed("safe text")

    qdrant_client = _clients_for_url(clients, "qdrant.invalid")[0]
    ollama_client = _clients_for_url(clients, "ollama.invalid")[0]
    _assert_custom_ca(qdrant_client.constructor_kwargs.get("verify"), ca_path)
    assert "verify" not in ollama_client.constructor_kwargs


def test_backend_failure_logs_redact_qdrant_key(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("QDRANT_API_KEY", QDRANT_KEY)
    _install_httpx_clients(monkeypatch, failure_secret=QDRANT_KEY)
    caplog.set_level(logging.WARNING, logger=qdrant_backend.__name__)
    backend = qdrant_backend.QdrantBackend(
        qdrant_url="https://qdrant.invalid",
        ollama_url="http://ollama.invalid",
    )

    assert backend.delete_claim(7) is False
    assert QDRANT_KEY not in caplog.text


def test_backend_constructor_error_redacts_qdrant_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QDRANT_API_KEY", QDRANT_KEY)

    def fail_client(**kwargs: Any) -> object:
        raise RuntimeError(f"synthetic failure: {QDRANT_KEY}")

    monkeypatch.setattr(qdrant_backend.httpx, "Client", fail_client)

    with pytest.raises(RuntimeError) as caught:
        qdrant_backend.QdrantBackend(qdrant_url="https://qdrant.invalid")

    assert QDRANT_KEY not in str(caught.value)


def test_backend_collection_error_redacts_qdrant_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QDRANT_API_KEY", QDRANT_KEY)
    _install_httpx_clients(monkeypatch, failure_secret=QDRANT_KEY)
    backend = qdrant_backend.QdrantBackend(qdrant_url="https://qdrant.invalid")

    with pytest.raises(RuntimeError) as caught:
        backend.ensure_collection()

    assert QDRANT_KEY not in str(caught.value)


@pytest.mark.parametrize(
    ("loader", "url_env"),
    [
        ("fallback", "MEMORYMASTER_QDRANT_URL"),
        ("indexer", None),
    ],
)
def test_qdrant_client_constructors_receive_api_key_and_ca(
    monkeypatch: pytest.MonkeyPatch,
    loader: str,
    url_env: str | None,
) -> None:
    ca_path = Path(certifi.where())
    monkeypatch.setenv("QDRANT_API_KEY", QDRANT_KEY)
    monkeypatch.setenv("QDRANT_CA_CERT", str(ca_path))
    url = "https://qdrant.invalid"
    if url_env:
        monkeypatch.setenv(url_env, url)
    observed: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> object:
        observed.append(kwargs)
        return object()

    _install_fake_qdrant_module(monkeypatch, factory)
    if loader == "fallback":
        assert qdrant_recall_fallback._get_client() is not None
    else:
        indexer = _load_indexer_without_rewrapping_pytest_stdout(monkeypatch)
        indexer._load_qdrant(url)

    assert observed[0]["api_key"] == QDRANT_KEY
    _assert_custom_ca(observed[0]["verify"], ca_path)


def test_fallback_constructor_error_does_not_log_qdrant_key(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("MEMORYMASTER_QDRANT_URL", "https://qdrant.invalid")
    monkeypatch.setenv("QDRANT_API_KEY", QDRANT_KEY)

    def factory(**kwargs: Any) -> object:
        raise RuntimeError(f"synthetic failure: {QDRANT_KEY}")

    _install_fake_qdrant_module(monkeypatch, factory)
    caplog.set_level(logging.WARNING, logger=qdrant_recall_fallback.__name__)

    assert qdrant_recall_fallback._get_client() is None
    assert QDRANT_KEY not in caplog.text


def test_indexer_upsert_error_does_not_log_qdrant_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    indexer = _load_indexer_without_rewrapping_pytest_stdout(monkeypatch)
    models = ModuleType("qdrant_client.models")
    models.PointStruct = lambda **kwargs: kwargs  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "qdrant_client.models", models)

    class _Vector(list):
        def tolist(self) -> list[float]:
            return list(self)

    class _Embedder:
        def get_sentence_embedding_dimension(self) -> int:
            return 2

        def encode(self, texts, **kwargs):
            return [_Vector([0.0, 0.0]) for _ in texts]

    monkeypatch.setattr(indexer, "_load_embedder", lambda model: _Embedder())
    monkeypatch.setattr(indexer, "_load_qdrant", lambda url: object())
    monkeypatch.setattr(indexer, "_ensure_collection", lambda *args: None)
    monkeypatch.setattr(indexer, "_count_claims", lambda path: 1)
    monkeypatch.setattr(
        indexer,
        "_iter_claims",
        lambda path: iter([(1, "project:test", "subject", "safe text", "confirmed", 0.9)]),
    )

    def fail_upsert(*args: Any) -> None:
        raise RuntimeError(f"synthetic failure: {QDRANT_KEY}")

    monkeypatch.setattr(indexer, "_upsert_batch", fail_upsert)
    caplog.set_level(logging.WARNING, logger="index_claims_to_qdrant")

    result = indexer.index_claims(
        tmp_path / "unused.db",
        "https://qdrant.invalid",
        "collection",
        "model",
        batch_size=1,
    )

    assert result["errors"] == 1
    assert QDRANT_KEY not in caplog.text


def test_indexer_collection_error_redacts_qdrant_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    indexer = _load_indexer_without_rewrapping_pytest_stdout(monkeypatch)

    class _Embedder:
        def get_sentence_embedding_dimension(self) -> int:
            return 2

    monkeypatch.setattr(indexer, "_load_embedder", lambda model: _Embedder())
    monkeypatch.setattr(indexer, "_load_qdrant", lambda url: object())

    def fail_collection(*args: Any) -> None:
        raise RuntimeError(f"synthetic failure: {QDRANT_KEY}")

    monkeypatch.setattr(indexer, "_ensure_collection", fail_collection)

    with pytest.raises(SystemExit) as caught:
        indexer.index_claims(
            tmp_path / "unused.db",
            "https://qdrant.invalid",
            "collection",
            "model",
        )

    assert QDRANT_KEY not in str(caught.value)


def test_dashboard_qdrant_probe_uses_key_and_ca_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QDRANT_API_KEY", QDRANT_KEY)
    monkeypatch.setenv("QDRANT_CA_CERT", certifi.where())
    observed: list[tuple[Any, Any]] = []

    def fake_urlopen(request, timeout, context=None):
        observed.append((request, context))
        return _Response(status=200)

    _install_qdrant_open(monkeypatch, fake_urlopen)

    assert dashboard._check_qdrant("https://qdrant.invalid")["status"] == "ok"
    request, context = observed[0]
    assert _request_headers(request).get("api-key") == QDRANT_KEY
    _assert_verified_context(context)


def test_setup_qdrant_probe_does_not_forward_key_to_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QDRANT_URL", "https://qdrant.invalid")
    monkeypatch.setenv("OLLAMA_URL", "https://ollama.invalid")
    monkeypatch.setenv("QDRANT_API_KEY", QDRANT_KEY)
    monkeypatch.setenv("QDRANT_CA_CERT", certifi.where())
    observed: list[tuple[Any, Any]] = []

    def fake_urlopen(request, timeout, context=None):
        observed.append((request, context))
        return _Response({"models": []})

    _install_qdrant_open(monkeypatch, fake_urlopen)
    monkeypatch.setattr(setup_detect.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(setup_detect, "_run", lambda args: None)

    assert setup_detect._probe_qdrant() is True
    assert setup_detect._probe_ollama()[0] is True
    qdrant_request, qdrant_context = next(item for item in observed if "qdrant.invalid" in _request_url(item[0]))
    ollama_request, _ = next(item for item in observed if "ollama.invalid" in _request_url(item[0]))
    assert _request_headers(qdrant_request).get("api-key") == QDRANT_KEY
    _assert_verified_context(qdrant_context)
    assert QDRANT_KEY not in repr(_request_headers(ollama_request))


def _create_verbatim_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE verbatim_memories (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            scope TEXT,
            timestamp TEXT,
            source_agent TEXT,
            embedding_synced INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        """INSERT INTO verbatim_memories
           (id, session_id, role, content, scope, timestamp, source_agent)
           VALUES (1, 'session', 'user', 'safe text', 'project:test',
                   '2026-07-11T00:00:00Z', 'pytest')"""
    )
    conn.commit()
    conn.close()


def test_verbatim_qdrant_requests_use_key_and_ca_but_openai_does_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "verbatim.db"
    _create_verbatim_db(db_path)
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    monkeypatch.setenv("QDRANT_API_KEY", QDRANT_KEY)
    monkeypatch.setenv("QDRANT_CA_CERT", certifi.where())
    monkeypatch.setattr(verbatim_store, "QDRANT_URL", "https://qdrant.invalid")
    observed: list[tuple[Any, Any]] = []

    def fake_urlopen(request, timeout, context=None):
        observed.append((request, context))
        if "api.openai.com" in _request_url(request):
            return _Response({"data": [{"embedding": [0.0] * verbatim_store.EMBED_DIM}]})
        return _Response()

    _install_qdrant_open(monkeypatch, fake_urlopen)
    monkeypatch.setattr(verbatim_store.urllib.request, "urlopen", fake_urlopen)

    assert verbatim_store.sync_to_qdrant(str(db_path)) == {"synced": 1}
    qdrant_calls = [item for item in observed if "qdrant.invalid" in _request_url(item[0])]
    openai_call = next(item for item in observed if "api.openai.com" in _request_url(item[0]))
    assert qdrant_calls
    for request, context in qdrant_calls:
        assert _request_headers(request).get("api-key") == QDRANT_KEY
        _assert_verified_context(context)
    assert QDRANT_KEY not in repr(_request_headers(openai_call[0]))


def test_dashboard_and_verbatim_errors_redact_qdrant_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "verbatim.db"
    _create_verbatim_db(db_path)
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    monkeypatch.setenv("QDRANT_API_KEY", QDRANT_KEY)
    monkeypatch.setattr(verbatim_store, "QDRANT_URL", "https://qdrant.invalid")

    def fail_urlopen(request, timeout, context=None):
        raise RuntimeError(f"synthetic failure: {QDRANT_KEY}")

    _install_qdrant_open(monkeypatch, fail_urlopen)
    dashboard_result = dashboard._check_qdrant("https://qdrant.invalid")
    verbatim_result = verbatim_store.sync_to_qdrant(str(db_path))

    assert QDRANT_KEY not in repr(dashboard_result)
    assert QDRANT_KEY not in repr(verbatim_result)


@pytest.mark.parametrize("surface", ["dashboard", "setup", "verbatim"])
def test_invalid_ca_stops_urllib_qdrant_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    missing_ca = tmp_path / "missing-ca.pem"
    monkeypatch.setenv("QDRANT_CA_CERT", str(missing_ca))
    monkeypatch.setenv("QDRANT_API_KEY", QDRANT_KEY)
    network_calls: list[str] = []

    def fail_if_called(request, timeout, context=None):
        network_calls.append(_request_url(request))
        return _Response()

    _install_qdrant_open(monkeypatch, fail_if_called)
    monkeypatch.setattr(dashboard.urllib.request, "urlopen", fail_if_called)
    if surface == "dashboard":
        assert dashboard._check_qdrant("https://qdrant.invalid")["status"] == "fail"
    elif surface == "setup":
        monkeypatch.setenv("QDRANT_URL", "https://qdrant.invalid")
        assert setup_detect._probe_qdrant() is False
    else:
        db_path = tmp_path / "verbatim.db"
        _create_verbatim_db(db_path)
        monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
        monkeypatch.setattr(verbatim_store, "QDRANT_URL", "https://qdrant.invalid")
        assert verbatim_store.sync_to_qdrant(str(db_path))["synced"] == 0

    assert network_calls == []
