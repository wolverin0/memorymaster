from __future__ import annotations

from pathlib import Path

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


QUERY_TOKEN = "federatedtenanttoken"


def _service(tmp_path: Path, monkeypatch) -> MemoryService:
    monkeypatch.delenv("QDRANT_URL", raising=False)
    svc = MemoryService(str(tmp_path / "memory.db"), workspace_root=tmp_path)
    svc.init_db()
    return svc


def _ingest(
    svc: MemoryService,
    text: str,
    *,
    scope: str,
    visibility: str = "public",
) -> None:
    svc.ingest(
        text=text,
        citations=[CitationInput(source="synthetic-test", locator=scope, excerpt=text)],
        scope=scope,
        claim_type="fact",
        visibility=visibility,
    )


def _result_texts(rows: list[dict]) -> set[str]:
    return {row["claim"].text for row in rows}


def test_sensitive_claim_in_other_scope_filtered(tmp_path, monkeypatch) -> None:
    svc = _service(tmp_path, monkeypatch)
    public_text = f"{QUERY_TOKEN} public foo claim"
    sensitive_text = f"{QUERY_TOKEN} sensitive foo claim"
    _ingest(svc, public_text, scope="project:foo")
    _ingest(svc, sensitive_text, scope="project:foo", visibility="sensitive")

    rows = svc.federated_query(QUERY_TOKEN, current_scope="project:bar", limit=10)

    assert _result_texts(rows) == {public_text}


def test_sensitive_claim_in_own_scope_included(tmp_path, monkeypatch) -> None:
    svc = _service(tmp_path, monkeypatch)
    public_text = f"{QUERY_TOKEN} public foo claim"
    sensitive_text = f"{QUERY_TOKEN} sensitive foo claim"
    _ingest(svc, public_text, scope="project:foo")
    _ingest(svc, sensitive_text, scope="project:foo", visibility="sensitive")

    rows = svc.federated_query(QUERY_TOKEN, current_scope="project:foo", limit=10)

    assert _result_texts(rows) == {public_text, sensitive_text}


def test_global_scope_with_sensitive_excluded_from_project_scope(tmp_path, monkeypatch) -> None:
    svc = _service(tmp_path, monkeypatch)
    public_text = f"{QUERY_TOKEN} public global claim"
    sensitive_text = f"{QUERY_TOKEN} sensitive global claim"
    _ingest(svc, public_text, scope="global")
    _ingest(svc, sensitive_text, scope="global", visibility="sensitive")

    rows = svc.federated_query(QUERY_TOKEN, current_scope="project:bar", limit=10)

    assert _result_texts(rows) == {public_text}


def test_team_scope_isolation_and_explicit_opt_in(tmp_path, monkeypatch) -> None:
    svc = _service(tmp_path, monkeypatch)
    alpha_text = f"{QUERY_TOKEN} public alpha team claim"
    beta_text = f"{QUERY_TOKEN} public beta team claim"
    _ingest(svc, alpha_text, scope="team:alpha")
    _ingest(svc, beta_text, scope="team:beta")

    beta_rows = svc.federated_query(QUERY_TOKEN, current_scope="team:beta", limit=10)
    alpha_rows = svc.federated_query(
        QUERY_TOKEN,
        current_scope="team:beta",
        scope_allowlist=["team:alpha"],
        limit=10,
    )

    assert _result_texts(beta_rows) == {beta_text}
    assert _result_texts(alpha_rows) == {alpha_text}
