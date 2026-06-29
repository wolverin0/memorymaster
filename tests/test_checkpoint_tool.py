"""checkpoint batch-ingest tool (plan 3.2b).

WHY this matters: the autoloop / a session filing many claims pays one MCP
round-trip per claim. `checkpoint` batches them — but a batch ingest path is
exactly where a sensitivity-filter bypass would hide, and where a partial
failure could silently drop claims. These tests anchor on the two invariants
that make batching safe: (1) the SAME per-item sensitivity filter runs on every
item (no bypass), and (2) every item's fate is reported (no silent-dropper) so a
half-failed batch is visible, not lost.

Borrowed from MemPalace's `checkpoint` batch-save (re-survey 2026-06-24).
"""
from __future__ import annotations

import pytest

from memorymaster.surfaces.mcp_server import _checkpoint_batch
from memorymaster.core.service import MemoryService


_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ."
    "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)


@pytest.fixture
def svc(tmp_path):
    s = MemoryService(db_target=str(tmp_path / "checkpoint.db"), workspace_root=tmp_path)
    s.init_db()
    return s


def _batch(svc, items):
    return _checkpoint_batch(
        svc, items, default_scope="project:test", workspace=".", source_agent="test-agent",
    )


def test_batch_ingests_all_valid_items(svc):
    """N valid claims in one call → N persisted, ids returned."""
    res = _batch(svc, [
        {"text": "Pedrito uses PostgreSQL"},
        {"text": "The deploy runs on Fridays", "claim_type": "fact"},
    ])
    assert res["ingested"] == 2
    assert len(res["claim_ids"]) == 2
    assert res["errors"] == []
    assert svc.store.get_claim(res["claim_ids"][0]) is not None


def test_sensitivity_filter_fires_per_item(svc):
    """THE invariant: a secret-bearing item is blocked by the SAME filter as
    ingest_claim — batching is not a bypass. The clean item still lands."""
    res = _batch(svc, [
        {"text": "A perfectly normal durable fact about the project"},
        {"text": f"the api token is {_JWT}"},
    ])
    assert res["ingested"] == 1
    assert res["skipped_sensitive"] == 1
    assert any(e["error"] == "sensitive_input_blocked" for e in res["errors"])


def test_partial_batch_reports_every_item_no_silent_drop(svc):
    """A mixed batch must surface each failure by index — never silently lose
    a claim the caller believes was filed."""
    res = _batch(svc, [
        {"text": "valid one"},
        {"text": ""},              # missing text
        {"no_text_key": "x"},      # missing text
        {"text": "valid two"},
        "not-an-object",           # wrong shape
    ])
    assert res["ingested"] == 2
    err_indexes = {e["index"] for e in res["errors"]}
    assert err_indexes == {1, 2, 4}


def test_empty_batch_is_a_clean_noop(svc):
    res = _batch(svc, [])
    assert res == {"ok": True, "ingested": 0, "skipped_sensitive": 0, "errors": [], "claim_ids": []}
