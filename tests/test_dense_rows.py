"""query_memory default = dense recall with lexical fallback (T-2115).

The dense client is monkeypatched: ids and scores are what the real service returns,
the store is a temp database. Vectors only PROPOSE ids; every contract below is about
what the store lets through.
"""
from __future__ import annotations

from typing import Any

import pytest

import memorymaster.core.access_control as access_control
import memorymaster.surfaces.mcp_server as mcp_server
from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall import dense_recall
from memorymaster.recall.dense_rows import fuse_rows


@pytest.fixture(autouse=True)
def isolated_auth_state(monkeypatch: pytest.MonkeyPatch):
    access_control._agent_roles.clear()
    monkeypatch.setattr(access_control, "_loaded", True)
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    yield
    access_control._agent_roles.clear()


class _Fixture:
    def __init__(self, root) -> None:
        root.mkdir()
        self.workspace = root / "allowed"
        self.workspace.mkdir()
        self.db = str(root / "dense.db")
        self.svc = MemoryService(self.db, workspace_root=self.workspace)
        self.svc.init_db()

    def claim(self, text: str, *, scope: str = "project:allowed", confirm: bool = True) -> int:
        claim = self.svc.ingest(text, [CitationInput(source="test://dense")], scope=scope, source_agent="seed")
        if confirm:
            transition_claim(self.svc.store, claim.id, "confirmed", reason="fixture", event_type="validator")
        return claim.id

    def query(self, **kwargs: Any) -> dict[str, Any]:
        args = {"query": "restart the bridge", "db": self.db, "workspace": str(self.workspace),
                "scope_allowlist": "project:allowed", **kwargs}
        return mcp_server.query_memory(**args)


@pytest.fixture
def fx(tmp_path, monkeypatch: pytest.MonkeyPatch) -> _Fixture:
    fixture = _Fixture(tmp_path / "main")
    monkeypatch.setattr(mcp_server, "_ENV_DEFAULT_DB", fixture.db)  # the db the dense service indexes
    return fixture


def _dense_answers(monkeypatch: pytest.MonkeyPatch, results: list[tuple[int, float]]) -> list[Any]:
    calls: list[Any] = []

    def search(query: str, scopes: Any, *, k: int = 12, outside_k: int = 0) -> dense_recall.DenseAnswer:
        calls.append((query, scopes))
        return dense_recall.DenseAnswer(tuple(results), 1.0, 1.0, len(results), "test-model")

    monkeypatch.setattr(dense_recall, "search", search)
    return calls


def _unavailable(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> list[Any]:
    calls: list[Any] = []

    def search(query: str, scopes: Any, **_kw: Any) -> Any:
        calls.append(query)
        raise exc

    monkeypatch.setattr(dense_recall, "search", search)
    return calls


def test_default_uses_dense_ids_rehydrated_from_the_store(fx, monkeypatch) -> None:
    wanted = fx.claim("semantic neighbour that shares no keyword with the query")
    _dense_answers(monkeypatch, [(wanted, 0.81)])

    result = fx.query()

    assert result["requested_retrieval_mode"] == "dense"
    assert result["retrieval_mode"] == "dense"
    assert "containment_reason" not in result
    assert [c["id"] for c in result["claims"]] == [wanted]
    assert result["rows_data"][0]["vector_score"] == pytest.approx(0.81)


def test_dense_ids_are_re_authorized_by_the_store(fx, monkeypatch) -> None:
    ok = fx.claim("restart the bridge after the gotcha")
    other_scope = fx.claim("restart the bridge in another project", scope="project:other")
    candidate = fx.claim("restart the bridge, unconfirmed", confirm=False)
    below_cutoff = fx.claim("restart the bridge but a weak cosine")
    _dense_answers(
        monkeypatch,
        [(other_scope, 0.95), (candidate, 0.9), (999_999, 0.88), (ok, 0.8), (below_cutoff, 0.3)],
    )

    result = fx.query(query="zzqq unrelated words")

    assert [c["id"] for c in result["claims"]] == [ok]
    assert result["retrieval_mode"] == "dense"


def test_dense_never_returns_a_sensitive_claim(fx, monkeypatch) -> None:
    ok = fx.claim("restart the bridge after the gotcha")
    leaked = fx.claim("restart the bridge, api_key=AKIAABCDEFGHIJKLMNOP")  # ingest redacts it: marker stays
    _dense_answers(monkeypatch, [(leaked, 0.95), (ok, 0.8)])

    result = fx.query(query="zzqq unrelated words")

    assert [c["id"] for c in result["claims"]] == [ok]


def test_dense_respects_per_agent_visibility(fx, monkeypatch) -> None:
    public = fx.claim("restart the bridge after the gotcha")
    private = fx.claim("restart the bridge, private note")
    with fx.svc.store.connect() as conn:  # fixture-only: no public API sets visibility after ingest
        conn.execute("UPDATE claims SET visibility='private', source_agent='someone-else' WHERE id=?", (private,))
    _dense_answers(monkeypatch, [(private, 0.95), (public, 0.8)])
    monkeypatch.setattr(mcp_server, "_team_request_principal", lambda: "reader-agent")

    result = fx.query(query="zzqq unrelated words")

    assert [c["id"] for c in result["claims"]] == [public]


def test_dense_is_asked_with_the_effective_scope_allowlist(fx, monkeypatch) -> None:
    claim = fx.claim("restart the bridge")
    calls = _dense_answers(monkeypatch, [(claim, 0.8)])

    fx.query()

    assert calls == [("restart the bridge", ["project:allowed"])]


@pytest.mark.parametrize(
    "exc, fragment",
    [
        (dense_recall.DenseUnavailable("URLError"), "unavailable"),
        (dense_recall.DenseCold("cold"), "warming up"),
    ],
)
def test_unavailable_dense_falls_back_to_lexical_and_says_why(fx, monkeypatch, exc, fragment) -> None:
    claim = fx.claim("restart the bridge after the gotcha")
    _unavailable(monkeypatch, exc)

    result = fx.query()

    assert result["requested_retrieval_mode"] == "dense"
    assert result["retrieval_mode"] == "legacy"
    assert fragment in result["containment_reason"]
    assert [c["id"] for c in result["claims"]] == [claim]


def test_no_dense_candidate_over_the_cutoff_answers_lexically(fx, monkeypatch) -> None:
    claim = fx.claim("restart the bridge after the gotcha")
    _dense_answers(monkeypatch, [(claim, 0.2)])

    result = fx.query()

    assert result["retrieval_mode"] == "legacy"
    assert "cosine" in result["containment_reason"]
    assert [c["id"] for c in result["claims"]] == [claim]


def test_dense_and_lexical_rows_are_fused_without_duplicates(fx, monkeypatch) -> None:
    both = fx.claim("restart the bridge after the gotcha")
    dense_only = fx.claim("completely different wording about reviving the link")
    lexical_only = fx.claim("restart the bridge twice")
    _dense_answers(monkeypatch, [(both, 0.8), (dense_only, 0.75)])

    result = fx.query(limit=10)

    ids = [c["id"] for c in result["claims"]]
    assert result["retrieval_mode"] == "dense"
    assert sorted(ids) == sorted({both, dense_only, lexical_only})
    assert ids[0] == both, "a claim both streams found ranks first"
    by_id = dict(zip(ids, result["rows_data"], strict=True))
    assert by_id[both]["vector_score"] > 0 and by_id[both]["lexical_score"] > 0


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"trust_mode": "exploratory", "include_candidates": True}, "confirmed claims only"),
        ({"allow_sensitive": True}, "allow_sensitive"),
    ],
)
def test_requests_dense_cannot_serve_stay_lexical(fx, monkeypatch, kwargs, fragment) -> None:
    fx.claim("restart the bridge after the gotcha")
    calls = _dense_answers(monkeypatch, [])
    monkeypatch.setattr(mcp_server, "resolve_allow_sensitive_access", lambda **_kw: True)

    result = fx.query(**kwargs)

    assert calls == []
    assert result["retrieval_mode"] == "legacy"
    assert fragment in result["containment_reason"]


def test_other_database_never_reaches_the_dense_service(fx, monkeypatch, tmp_path) -> None:
    """The service indexes one db; its ids mean nothing against another file."""
    other = _Fixture(tmp_path / "second")
    other.claim("restart the bridge after the gotcha")
    calls = _dense_answers(monkeypatch, [(1, 0.99)])

    result = other.query()

    assert calls == []
    assert result["retrieval_mode"] == "legacy"
    assert "default database" in result["containment_reason"]


def test_explicit_legacy_is_unchanged(fx, monkeypatch) -> None:
    claim = fx.claim("restart the bridge after the gotcha")
    calls = _dense_answers(monkeypatch, [(claim, 0.9)])

    result = fx.query(retrieval_mode="legacy")

    assert calls == []
    assert result["requested_retrieval_mode"] == "legacy"
    assert result["retrieval_mode"] == "legacy"
    assert "containment_reason" not in result


def test_team_mode_default_is_lexical_not_denied(fx, monkeypatch) -> None:
    """Team mode keeps semantic retrieval off; the new default must degrade, not raise."""
    context = type("Ctx", (), {"mode": mcp_server.AuthMode.TEAM})()
    monkeypatch.setattr(mcp_server, "current_request_context", lambda: context)

    assert "team mode" in mcp_server._dense_block_reason(fx.db)


def test_fuse_rows_keeps_both_signals_and_orders_by_rank() -> None:
    def row(cid: int, **extra: Any) -> dict[str, Any]:
        claim = type("C", (), {"id": cid})()
        return {"claim": claim, "score": 0.0, "lexical_score": 0.0, "vector_score": 0.0, "breakdown": {}, **extra}

    dense = [row(1, vector_score=0.8), row(2, vector_score=0.7)]
    lexical = [row(2, lexical_score=0.4), row(3, lexical_score=0.3)]

    fused = fuse_rows(dense, lexical, 3)

    assert [r["claim"].id for r in fused] == [2, 1, 3]
    assert fused[0]["vector_score"] == 0.7 and fused[0]["lexical_score"] == 0.4
    assert fuse_rows(dense, lexical, 2)[-1]["claim"].id == 1
