from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest


PLUGIN_SRC = Path(__file__).parents[1] / "integrations" / "hermes-memorymaster" / "src"
if str(PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(PLUGIN_SRC))

from hermes_memorymaster.backend import (  # noqa: E402
    BackendAuthError,
    BackendPayloadError,
    MCPHttpBackend,
    ReadOnlyReplicaBackend,
    _classify_message,
)
from hermes_memorymaster.config import ProviderConfig  # noqa: E402
from hermes_memorymaster.provider import MemoryMasterProvider  # noqa: E402
from memorymaster.capture.worker import run_capture_worker  # noqa: E402
from memorymaster.core.models import CitationInput  # noqa: E402
from memorymaster.core.service import MemoryService  # noqa: E402
from memorymaster.knowledge.skill_schema import build_skill_fields  # noqa: E402


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_until_healthy(
    health_url: str, *, budget_seconds: float = 30.0, poll_seconds: float = 0.05
) -> bool:
    """Espera a que el servidor responda 200, durmiendo en TODOS los caminos.

    El bucle anterior dormia solo en el `except`: si el servidor aceptaba la
    conexion pero devolvia un estado distinto de 200 —arrancando— giraba sus 100
    vueltas en milisegundos y se rendia sin haber esperado nada. Y con 100
    iteraciones fijas el presupuesto real dependia de si cada intento fallaba
    rapido o agotaba su timeout, o sea que no habia presupuesto.

    Ahora el limite es TIEMPO, no vueltas. Importa porque el sintoma no es un
    error de arranque legible: el test avanza, la llamada MCP falla, y
    `_classify_transport_error` la reporta como `authority_unavailable`, que es
    su fallback para cualquier error de transporte sin clasificar. Cuatro caidas
    en CI el 2026-08-31, todas en Windows, entre 1 y 1,5 h de suite cada una.

    Esto NO es un reintento del test: no repite aserciones ni tolera un fallo
    real. Solo le da al arranque el tiempo que el bucle decia darle y no daba.
    """
    deadline = time.monotonic() + budget_seconds
    while time.monotonic() < deadline:
        try:
            if httpx.get(health_url, timeout=1.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(poll_seconds)
    return False


@pytest.fixture
def mcp_http_server(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = tmp_path / "authority.db"
    token = "fixture-bearer-token"
    port = _free_port()
    MemoryService(db, workspace_root=workspace).init_db()
    environment = os.environ.copy()
    environment.update(
        {
            "MEMORYMASTER_MCP_AUTH_MODE": "team",
            "MEMORYMASTER_MCP_HTTP_TOKEN": token,
            "MEMORYMASTER_MCP_HTTP_ALLOWED_HOSTS": f"127.0.0.1:{port}",
            "MEMORYMASTER_DEFAULT_DB": str(db),
            "MEMORYMASTER_WORKSPACE": str(workspace),
            "MEMORYMASTER_MCP_PRINCIPAL": "hermes-memorymaster",
            "MEMORYMASTER_ROLE_HERMES_MEMORYMASTER": "writer",
            "MEMORYMASTER_MCP_TENANT_ID": "fixture-tenant",
            "MEMORYMASTER_MCP_WORKSPACE": str(workspace),
            "MEMORYMASTER_MCP_ALLOWED_SCOPES": "project:workspace",
            "MEMORYMASTER_MCP_DB": str(db),
            "PYTHONPATH": os.pathsep.join(
                filter(
                    None,
                    (str(Path(__file__).parents[1]), environment.get("PYTHONPATH", "")),
                )
            ),
        }
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "memorymaster.surfaces.mcp_http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--db",
            str(db),
            "--workspace",
            str(workspace),
        ],
        cwd=workspace,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    health = f"http://127.0.0.1:{port}/healthz"
    if not _wait_until_healthy(health):
        raise AssertionError("disposable MemoryMaster MCP server did not start")
    yield f"http://127.0.0.1:{port}/mcp", token, db, workspace
    process.terminate()
    process.wait(timeout=3.0)


def test_authenticated_mcp_http_delivers_disposable_capture(
    mcp_http_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint, token, db, workspace = mcp_http_server
    config = ProviderConfig(
        endpoint=endpoint,
        token=token,
        outbox_path=tmp_path / "outbox.db",
        default_scope="project:workspace",
        worker_enabled=False,
    )
    provider = MemoryMasterProvider(
        config=config,
        backend=MCPHttpBackend(
            endpoint,
            token,
            timeout_seconds=config.request_timeout_seconds,
            delivery_timeout_seconds=config.delivery_timeout_seconds,
        ),
    )
    provider.initialize(
        "raw-session",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="otacon",
    )
    bound = provider.backend.scope(
        "bind",
        session_id=provider.session_hash,
        source_agent=provider.source_agent,
        platform=provider.platform,
        scope="project:workspace",
        task_label="fixture",
    )
    shown = json.loads(provider.handle_tool_call("memorymaster_scope", {"action": "show"}))
    assert bound.get("ok") is True, bound
    assert bound["scope"] == "project:workspace"
    assert shown["rows"] == 1
    provider.on_turn_start(4, "capture")
    provider.sync_turn("The fixture project uses SQLite.", "Recorded.")

    assert provider.drain_once() is True
    status = provider.status()
    assert status["last_error_code"] is None, status
    assert status["completed"] == 1, status
    service = MemoryService(db, workspace_root=workspace, read_only=True)
    with service.store.connect() as connection:
        source = connection.execute(
            "SELECT id, source_item_id, payload_json FROM source_items"
        ).fetchone()
        evidence = connection.execute("SELECT COUNT(*) FROM evidence_items").fetchone()[0]
    assert source[1].startswith("producer:hermes:")
    assert "raw-session" not in source[2]
    assert evidence == 1
    preview = provider.backend.forget_preview(source_item_id=int(source[0]))
    assert preview["apply"] is False
    assert preview["evidence_preserved"] is True

    def fake_call(_prompt: str, _text: str) -> str:
        return json.dumps(
            [
                {
                    "type": "project",
                    "subject": "Fixture project",
                    "predicate": "uses",
                    "object": "SQLite",
                    "text": "The fixture project uses SQLite.",
                    "confidence": 0.95,
                }
            ]
        )

    monkeypatch.setattr("memorymaster.bridges.atlas_llm_extractor.call_llm", fake_call)
    worker = run_capture_worker(
        MemoryService(db, workspace_root=workspace),
        owner="hermes-http-fixture",
        limit=1,
    )
    assert worker.completed == 1
    with MemoryService(db, workspace_root=workspace, read_only=True).store.connect() as connection:
        lineage = connection.execute(
            """SELECT c.status, j.status, COUNT(*) AS links
               FROM claims c
               JOIN claim_evidence_links cel ON cel.claim_id=c.id
               JOIN evidence_items e ON e.id=cel.evidence_item_id
               JOIN capture_jobs j ON j.source_item_id=e.source_item_id
               GROUP BY c.id, j.id"""
        ).fetchone()
    assert tuple(lineage) == ("candidate", "completed", 1)

    provider.on_session_end([])
    assert provider.status()["completed"] == 2
    cleared = json.loads(provider.handle_tool_call("memorymaster_scope", {"action": "clear"}))
    assert cleared["ended"] == 1
    provider.close_outbox()


def test_mcp_http_rejects_wrong_token_as_permanent_auth_error(mcp_http_server) -> None:
    endpoint, _token, _db, _workspace = mcp_http_server
    backend = MCPHttpBackend(endpoint, "wrong-token", timeout_seconds=2.0)
    with pytest.raises(BackendAuthError):
        backend.recall("fixture", scope="user", session_id="a" * 64)


def test_mcp_http_uses_longer_timeout_for_durable_delivery(monkeypatch) -> None:
    backend = MCPHttpBackend(
        "https://memory.invalid/mcp",
        "fixture-token",
        timeout_seconds=0.35,
        delivery_timeout_seconds=5.0,
    )
    seen = []

    async def fake_call(tool_name, arguments, *, timeout_seconds):
        seen.append((tool_name, timeout_seconds))
        return {"output": "context"}

    monkeypatch.setattr(backend, "_call_async", fake_call)
    backend.recall("fixture", scope="user", session_id="a" * 64)
    backend.remember(
        {
            "payload": {"text": "fixture", "scope": "user"},
            "identity": {
                "source_agent": "fixture",
                "session_hash": "a" * 64,
                "external_id": "fixture",
                "content_hash": "b" * 64,
                "turn_id": "1",
            },
        }
    )
    backend.improve(scope="user", max_items=1)

    assert seen == [("recall", 0.35), ("remember", 5.0), ("improve", 5.0)]


def test_mcp_http_uses_one_bounded_stateless_jsonrpc_post(monkeypatch) -> None:
    seen = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self):
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"structuredContent": {"ok": True}},
            }

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            seen["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, endpoint, *, json):
            seen["post"] = (endpoint, json)
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)
    backend = MCPHttpBackend(
        "https://memory.invalid/mcp",
        "fixture-token",
        timeout_seconds=0.35,
    )

    result = asyncio.run(
        backend._call_async("recall", {"query": "fixture"}, timeout_seconds=0.35)
    )

    assert result == {"ok": True}
    assert seen["client"]["timeout"].read == 0.35
    assert seen["post"][0] == "https://memory.invalid/mcp"
    assert seen["post"][1]["method"] == "tools/call"
    assert seen["post"][1]["params"] == {
        "name": "recall",
        "arguments": {"query": "fixture"},
    }


def test_unsafe_persisted_data_is_a_permanent_payload_rejection() -> None:
    error = _classify_message(
        "Error executing tool remember: Source item contains unsafe persisted data."
    )

    assert isinstance(error, BackendPayloadError)
    assert error.code == "unsafe_persisted_data"


def test_replica_recall_leaves_sqlite_bytes_unchanged(tmp_path: Path) -> None:
    db = tmp_path / "replica.db"
    service = MemoryService(db, workspace_root=tmp_path)
    service.init_db()
    claim = service.ingest(
        text="Replica fixture memory",
        citations=[CitationInput(source="fixture", locator="fixture")],
        scope="user",
        source_agent="fixture",
    )
    service.store.apply_status_transition(
        claim,
        to_status="confirmed",
        reason="fixture",
        event_type="validator",
    )
    with service.store.connect() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    context = ReadOnlyReplicaBackend(db, tmp_path).recall(
        "Replica fixture", scope="user", session_id="a" * 64
    )

    assert "Replica fixture memory" in context
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def _confirmed_skill(service: MemoryService, *, scope: str) -> int:
    fields = build_skill_fields(
        {
            "schema": "personal-skill-v1",
            "slug": "recover-provider",
            "title": "Recover provider",
            "when_to_use": "Use when the memory provider stops responding.",
            "when_not_to_use": "Do not use when the provider is healthy.",
            "inputs": ["provider status"],
            "prerequisites": ["service access"],
            "workflow": ["Inspect provider state", "Restart only the failed service"],
            "decision_rules": ["Never reboot the host before targeted recovery"],
            "expected_output": "A healthy provider with direct evidence.",
            "validation": ["Provider status is active"],
            "pitfalls": ["Restarting unrelated containers"],
            "recovery": ["Use the documented rollback"],
            "quality_scores": {
                "recurrence": 16,
                "reusability": 16,
                "executability": 16,
                "validation": 16,
                "safety": 16,
            },
        },
        supporting_claim_ids=[11, 12],
    )
    claim = service.ingest(
        **fields,
        citations=[CitationInput(source="fixture", locator="skill")],
        scope=scope,
        source_agent="fixture",
    )
    service.store.apply_status_transition(
        claim,
        to_status="confirmed",
        reason="fixture approval",
        event_type="validator",
    )
    return claim.id


def test_authoritative_hermes_recall_injects_confirmed_skill(mcp_http_server) -> None:
    endpoint, token, db, workspace = mcp_http_server
    service = MemoryService(db, workspace_root=workspace)
    claim_id = _confirmed_skill(service, scope="project:workspace")

    context = MCPHttpBackend(endpoint, token, timeout_seconds=10.0).recall(
        "recover provider",
        scope="project:workspace",
        session_id="a" * 64,
    )

    assert "=== APPROVED SKILLS ===" in context
    assert "Restart only the failed service" in context
    assert str(claim_id) in context


def test_replica_recall_injects_confirmed_skill_without_writing(tmp_path: Path) -> None:
    db = tmp_path / "replica-skill.db"
    service = MemoryService(db, workspace_root=tmp_path)
    service.init_db()
    _confirmed_skill(service, scope="user")
    with service.store.connect() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    context = ReadOnlyReplicaBackend(db, tmp_path).recall(
        "recover provider",
        scope="user",
        session_id="a" * 64,
    )

    assert "=== APPROVED SKILLS ===" in context
    assert "Restart only the failed service" in context
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
