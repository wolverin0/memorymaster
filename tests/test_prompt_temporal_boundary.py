"""All prompt candidate streams must obey current claim validity."""

from types import SimpleNamespace

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall import context_hook


@pytest.mark.parametrize(
    "start,end,allowed",
    [
        (None, None, True),
        ("2020-01-01T00:00:00Z", "2099-01-01T00:00:00Z", True),
        (None, "2020-01-01T00:00:00Z", False),
        ("2099-01-01T00:00:00Z", None, False),
        ("invalid", None, False),
        (None, "invalid", False),
    ],
)
def test_prompt_boundary_checks_temporal_truth(start, end, allowed):
    claim = SimpleNamespace(
        status="confirmed", scope="project:test", visibility="public",
        text="Use the documented build command.", claim_type="constraint",
        valid_from=start, valid_until=end,
        object_value=None, subject=None, predicate=None,
    )
    plan = SimpleNamespace(statuses=("confirmed",), scope_allowlist=("project:test",))
    assert context_hook._prompt_claim_allowed(claim, plan) is allowed


def test_graph_stream_cannot_reintroduce_expired_or_future_claims(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_JEV_MODE", "off")
    monkeypatch.setenv("MEMORYMASTER_SCOPE_DEFAULT", "project:allowed")
    monkeypatch.setenv("MEMORYMASTER_SPOOL_DIR", str(tmp_path / "spool"))
    for flag in ("MEMORYMASTER_RECALL_GRAPH", "MEMORYMASTER_RECALL_GRAPH_CANDIDATES",
                 "MEMORYMASTER_RECALL_W_GRAPH"):
        monkeypatch.setenv(flag, "1")
    monkeypatch.delenv("QDRANT_URL", raising=False)
    db_path = tmp_path / "temporal-graph.db"
    service = MemoryService(db_path, workspace_root=tmp_path)
    service.init_db()

    def add(text, **bounds):
        claim = service.ingest(
            text, [CitationInput(source="pytest", locator="temporal-graph")],
            scope="project:allowed", volatility="medium", **bounds,
        )
        return service.store.apply_status_transition(
            claim, to_status="confirmed", reason="temporal fixture", event_type="validator",
        )

    anchor = add("temporalgraphanchor build uses the documented command.")
    current = add("Current graph-only fact uses the stable compiler.")
    expired = add("Expired graph-only fact uses an obsolete compiler.",
                  valid_until="2020-01-01T00:00:00Z")
    future = add("Future graph-only fact uses an unreleased compiler.",
                 valid_from="2099-01-01T00:00:00Z")
    monkeypatch.setattr(context_hook, "_graph_reached_claim_distance",
                        lambda _query, _store: {c.id: 1 for c in (current, expired, future)})
    rendered, ids = context_hook.recall(
        "temporalgraphanchor", db_path=str(db_path), skip_qdrant=True, return_ids=True,
    )
    assert anchor.id in ids
    assert current.id in ids
    assert expired.id not in ids
    assert future.id not in ids
    assert "Expired graph-only" not in rendered
    assert "Future graph-only" not in rendered
