from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import memorymaster.core.access_control as access_control
import memorymaster.core.service as service_module
import memorymaster.surfaces.mcp_server as mcp_server
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.stores.postgres_store import PostgresStore
from memorymaster.stores.store_factory import create_store


TEAM_PRINCIPAL = "mcp-writer"
TEAM_TENANT = "tenant-alpha"
TEAM_SCOPES = frozenset({"project:alpha", "global"})


class RecordingStore:
    def __init__(self) -> None:
        self.init_called = False
        self.list_claims_calls: list[dict[str, Any]] = []

    def init_db(self) -> None:
        self.init_called = True

    def list_claims(self, **kwargs: Any) -> list[Any]:
        self.list_claims_calls.append(kwargs)
        return []


@pytest.fixture(autouse=True)
def isolated_authority() -> None:
    access_control._agent_roles.clear()
    previous_loaded = access_control._loaded
    access_control._loaded = True
    yield
    access_control._agent_roles.clear()
    access_control._loaded = previous_loaded


@pytest.fixture
def team_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> dict[str, str]:
    workspace = tmp_path / "alpha"
    workspace.mkdir()
    values = {
        "MEMORYMASTER_MCP_AUTH_MODE": "team",
        "MEMORYMASTER_MCP_PRINCIPAL": TEAM_PRINCIPAL,
        "MEMORYMASTER_MCP_TENANT_ID": TEAM_TENANT,
        "MEMORYMASTER_MCP_WORKSPACE": str(workspace),
        "MEMORYMASTER_MCP_ALLOWED_SCOPES": "project:alpha,global",
        "MEMORYMASTER_MCP_DB": "postgresql://memorymaster.invalid/app",
    }
    access_control.set_role(TEAM_PRINCIPAL, access_control.Role.WRITER)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def _team_context(*, scopes: frozenset[str] = TEAM_SCOPES) -> access_control.RequestContext:
    return access_control.RequestContext(
        mode=access_control.AuthMode.TEAM,
        principal=TEAM_PRINCIPAL,
        role=access_control.Role.WRITER,
        tenant_id=TEAM_TENANT,
        workspace="C:/work/alpha",
        allowed_scopes=scopes,
        allow_sensitive=False,
        db_target="postgresql://memorymaster.invalid/app",
    )


def _service_with_recording_store(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MemoryService, RecordingStore]:
    store = RecordingStore()
    monkeypatch.setattr(service_module, "create_store", lambda *_args, **_kwargs: store)
    monkeypatch.setattr(MemoryService, "_init_qdrant", staticmethod(lambda: None))
    service = MemoryService(
        "postgresql://memorymaster.invalid/app",
        tenant_id=TEAM_TENANT,
        require_tenant=True,
        principal=TEAM_PRINCIPAL,
        allowed_scopes=TEAM_SCOPES,
    )
    return service, store


def test_resolved_request_context_uses_an_immutable_scope_grant(
    team_environment: dict[str, str],
) -> None:
    context = access_control.resolve_request_context(environ=team_environment)

    assert context.principal == TEAM_PRINCIPAL
    assert context.allowed_scopes == TEAM_SCOPES
    assert isinstance(context.allowed_scopes, frozenset)


def test_local_request_context_keeps_an_explicit_empty_frozen_grant() -> None:
    context = access_control.resolve_request_context(
        db_target="memorymaster.db",
        workspace="C:/work/alpha",
        environ={"MEMORYMASTER_MCP_AUTH_MODE": "local-trusted"},
    )

    assert context.mode is access_control.AuthMode.LOCAL_TRUSTED
    assert context.principal == "mcp-session"
    assert context.allowed_scopes == frozenset()
    assert isinstance(context.allowed_scopes, frozenset)


def test_mcp_service_propagates_bound_principal_and_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_service(db_target: str, **kwargs: Any) -> SimpleNamespace:
        captured.update({"db_target": db_target, **kwargs})
        return SimpleNamespace(source_agent=None)

    monkeypatch.setattr(mcp_server, "MemoryService", fake_service)
    monkeypatch.setattr(mcp_server, "_resolve_db", lambda value: value)
    monkeypatch.setattr(mcp_server, "_resolve_workspace", lambda value: value)
    monkeypatch.setattr(mcp_server, "_bind_telemetry_session", lambda *_args: None)

    context = _team_context()
    with access_control.bind_request_context(context):
        service = mcp_server._service(context.db_target, context.workspace)

    assert captured["tenant_id"] == TEAM_TENANT
    assert captured["require_tenant"] is True
    assert captured["principal"] == TEAM_PRINCIPAL
    assert captured["allowed_scopes"] == TEAM_SCOPES
    assert isinstance(captured["allowed_scopes"], frozenset)
    assert service.source_agent == TEAM_PRINCIPAL


def test_memory_service_propagates_authority_to_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_create_store(db_target: str, **kwargs: Any) -> RecordingStore:
        captured.update({"db_target": db_target, **kwargs})
        return RecordingStore()

    monkeypatch.setattr(service_module, "create_store", fake_create_store)
    monkeypatch.setattr(MemoryService, "_init_qdrant", staticmethod(lambda: None))

    service = MemoryService(
        "postgresql://memorymaster.invalid/app",
        tenant_id=TEAM_TENANT,
        require_tenant=True,
        principal=TEAM_PRINCIPAL,
        allowed_scopes=TEAM_SCOPES,
    )

    assert service.principal == TEAM_PRINCIPAL
    assert service.allowed_scopes == TEAM_SCOPES
    assert isinstance(service.allowed_scopes, frozenset)
    assert captured["principal"] == TEAM_PRINCIPAL
    assert captured["allowed_scopes"] == TEAM_SCOPES


def test_store_factory_propagates_immutable_authority_to_postgres() -> None:
    store = create_store(
        "postgresql://memorymaster.invalid/app",
        tenant_id=TEAM_TENANT,
        require_tenant=True,
        principal=TEAM_PRINCIPAL,
        allowed_scopes=TEAM_SCOPES,
    )

    assert isinstance(store, PostgresStore)
    assert store.principal == TEAM_PRINCIPAL
    assert store.allowed_scopes == TEAM_SCOPES
    assert isinstance(store.allowed_scopes, frozenset)


@pytest.mark.parametrize(
    ("authority", "message"),
    [
        (
            {
                "tenant_id": None,
                "principal": TEAM_PRINCIPAL,
                "allowed_scopes": TEAM_SCOPES,
            },
            "tenant",
        ),
        (
            {
                "tenant_id": TEAM_TENANT,
                "principal": None,
                "allowed_scopes": TEAM_SCOPES,
            },
            "principal",
        ),
        (
            {
                "tenant_id": TEAM_TENANT,
                "principal": TEAM_PRINCIPAL,
                "allowed_scopes": frozenset(),
            },
            "scope",
        ),
        (
            {
                "tenant_id": TEAM_TENANT,
                "principal": TEAM_PRINCIPAL,
                "allowed_scopes": frozenset({"*"}),
            },
            "wildcard|scope",
        ),
    ],
)
def test_postgres_team_authority_fails_closed_before_loading_driver(
    monkeypatch: pytest.MonkeyPatch,
    authority: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(PermissionError, match=message):
        store = PostgresStore(
            "postgresql://memorymaster.invalid/app",
            require_tenant=True,
            **authority,
        )
        monkeypatch.setattr(
            store,
            "_load_psycopg",
            lambda: pytest.fail("invalid team authority reached the Postgres driver"),
        )
        store.connect()


def test_invalid_team_service_authority_fails_before_qdrant_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        MemoryService,
        "_init_qdrant",
        staticmethod(lambda: pytest.fail("invalid team authority reached Qdrant")),
    )

    with pytest.raises(PermissionError, match="principal"):
        MemoryService(
            "postgresql://memorymaster.invalid/app",
            tenant_id=TEAM_TENANT,
            require_tenant=True,
            principal=None,
            allowed_scopes=TEAM_SCOPES,
        )


def test_mcp_query_scope_contract_defaults_narrows_and_rejects_widening(
    team_environment: dict[str, str],
) -> None:
    def probe(
        scope_allowlist: str = "",
        db: str = "memorymaster.db",
        workspace: str = ".",
    ) -> str:
        return scope_allowlist

    guarded = mcp_server._authorized_tool_callable(
        probe,
        mcp_server.McpToolPolicy("query", team_enabled=True),
    )

    assert guarded() == ",".join(sorted(TEAM_SCOPES))
    assert guarded(scope_allowlist="project:alpha") == "project:alpha"
    with pytest.raises(PermissionError, match="scope"):
        guarded(scope_allowlist="project:alpha,project:beta")


def test_service_query_defaults_to_bound_scope_and_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store = _service_with_recording_store(monkeypatch)
    observed_agents: list[str | None] = []

    def record_visibility(claims: list[Any], requesting_agent: str | None) -> list[Any]:
        observed_agents.append(requesting_agent)
        return claims

    monkeypatch.setattr(service_module, "_filter_agent_visibility", record_visibility)

    service.query_rows("authority marker", include_candidates=True)

    assert set(store.list_claims_calls[0]["scope_allowlist"]) == TEAM_SCOPES
    assert observed_agents == [TEAM_PRINCIPAL]


def test_service_query_allows_scope_narrowing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store = _service_with_recording_store(monkeypatch)

    service.query_rows(
        "authority marker",
        include_candidates=True,
        scope_allowlist=["project:alpha"],
    )

    assert store.list_claims_calls[0]["scope_allowlist"] == ["project:alpha"]


def test_service_query_rejects_scope_widening_before_store_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store = _service_with_recording_store(monkeypatch)

    with pytest.raises(PermissionError, match="scope"):
        service.query_rows(
            "authority marker",
            scope_allowlist=["project:alpha", "project:beta"],
        )

    assert store.list_claims_calls == []


def test_service_query_rejects_requesting_agent_substitution_before_store_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store = _service_with_recording_store(monkeypatch)

    with pytest.raises(PermissionError, match="principal|requesting_agent"):
        service.query_rows("authority marker", requesting_agent="forged-agent")

    assert store.list_claims_calls == []


def test_service_ingest_binds_principal_and_rejects_attribution_or_scope_forgery(
    tmp_path,
) -> None:
    db_path = tmp_path / "authority.db"
    local = MemoryService(db_path, workspace_root=tmp_path)
    local.init_db()
    service = MemoryService(
        db_path,
        workspace_root=tmp_path,
        tenant_id=TEAM_TENANT,
        require_tenant=True,
        principal=TEAM_PRINCIPAL,
        allowed_scopes=TEAM_SCOPES,
    )

    claim = service.ingest(
        "bound service authority marker",
        [CitationInput(source="test://authority")],
        scope="project:alpha",
    )
    before = len(service.store.list_claims(limit=20, tenant_id=TEAM_TENANT))

    assert claim.source_agent == TEAM_PRINCIPAL
    assert claim.scope == "project:alpha"
    with pytest.raises(PermissionError, match="principal|source_agent"):
        service.ingest(
            "forged source authority marker",
            [CitationInput(source="test://authority")],
            scope="project:alpha",
            source_agent="forged-agent",
        )
    with pytest.raises(PermissionError, match="scope"):
        service.ingest(
            "forged scope authority marker",
            [CitationInput(source="test://authority")],
            scope="project:beta",
        )
    assert len(service.store.list_claims(limit=20, tenant_id=TEAM_TENANT)) == before


def test_team_service_init_db_is_rejected_before_store_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RecordingStore()
    monkeypatch.setattr(service_module, "create_store", lambda *_args, **_kwargs: store)
    monkeypatch.setattr(MemoryService, "_init_qdrant", staticmethod(lambda: None))
    service = MemoryService(
        "postgresql://memorymaster.invalid/app",
        tenant_id=TEAM_TENANT,
        require_tenant=True,
        principal=TEAM_PRINCIPAL,
        allowed_scopes=TEAM_SCOPES,
    )

    with pytest.raises(PermissionError, match="team|schema|init"):
        service.init_db()

    assert store.init_called is False


def test_unbound_local_service_preserves_legacy_authority_compatibility(tmp_path) -> None:
    service = MemoryService(tmp_path / "local.db", workspace_root=tmp_path)
    service.init_db()

    claim = service.ingest(
        "local trusted compatibility marker",
        [CitationInput(source="test://local")],
        scope="project:local-custom",
        source_agent="local-tool",
    )
    rows = service.query_rows(
        "local trusted compatibility marker",
        include_candidates=True,
        scope_allowlist=["project:local-custom"],
        requesting_agent="local-tool",
    )

    assert claim.scope == "project:local-custom"
    assert claim.source_agent == "local-tool"
    assert [row["claim"].id for row in rows] == [claim.id]
