"""G-4: the daily supersession bounds cannot be bypassed by rotating names, the legacy argument or prose."""
from __future__ import annotations

import pytest

from memorymaster.core import lifecycle
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern import ingest_governance as ig

SCOPE = "project:g4"
CIT = [CitationInput(source="test://g4", locator="x")]


@pytest.fixture()
def svc(tmp_path):
    s = MemoryService(tmp_path / "g4.db", workspace_root=tmp_path)
    s.init_db()
    return s


def _victims(svc, n, scope=SCOPE):
    out = []
    for i in range(n):
        c = svc.ingest(f"Victim fact {i} about ring {i * 13 + 7} deployment", CIT, scope=scope, source_agent="victim")
        lifecycle.transition_claim(svc.store, c.id, "confirmed", reason="fixture", event_type="validator")
        out.append(c)
    return out


def _proposal_count(svc, source):
    con = svc.store.connect()
    return con.execute(
        "select count(distinct claim_id) from events where details='steward_proposal:superseded_candidate' "
        "and payload_json like ?", (f'%"source": "{source}"%',)).fetchone()[0]


def test_rotating_agent_names_hit_the_scope_bound(svc, monkeypatch):
    monkeypatch.setattr(ig, "MAX_DECLARED_PER_SCOPE_PER_DAY", 3)
    v = _victims(svc, 5)
    for k in range(3):
        svc.ingest(f"rot note {k} nothing", CIT, scope=SCOPE, source_agent=f"evil-{k}", supersedes=[v[k].id])
    with pytest.raises(ValueError, match="scope"):
        svc.ingest("rot note 3 nothing", CIT, scope=SCOPE, source_agent="evil-3", supersedes=[v[3].id])
    # another scope has its own budget
    other = _victims(svc, 1, scope="project:other")
    svc.ingest("other scope note", CIT, scope="project:other", source_agent="evil-9", supersedes=[other[0].id])


def test_legacy_single_argument_counts_toward_the_agent_bound(svc, monkeypatch):
    monkeypatch.setattr(ig, "MAX_DECLARED_PER_AGENT_PER_DAY", 3)
    v = _victims(svc, 5)
    for k in range(3):
        svc.ingest(f"legacy note {k} nothing", CIT, scope=SCOPE, source_agent="evil", supersedes_claim_id=v[k].id)
    with pytest.raises(ValueError, match="supersedes limit reached"):
        svc.ingest("legacy note 3 nothing", CIT, scope=SCOPE, source_agent="evil", supersedes_claim_id=v[3].id)
    assert _proposal_count(svc, "direct-ingest") == 3


def test_prose_corrections_count_toward_the_agent_bound(svc, monkeypatch):
    monkeypatch.setattr(ig, "MAX_DECLARED_PER_AGENT_PER_DAY", 3)
    v = _victims(svc, 6)
    for k in range(6):
        svc.ingest(f"Correction {k}: this supersedes {v[k].human_id} because the ring changed", CIT, scope=SCOPE,
                   source_agent="evil-prose")
    assert _proposal_count(svc, "prose-correction") == 3


def test_a_prose_correction_stopped_by_the_bound_is_reported(svc, monkeypatch):
    monkeypatch.setattr(ig, "MAX_DECLARED_PER_AGENT_PER_DAY", 1)
    v = _victims(svc, 2)
    first, second = {}, {}
    svc.ingest(f"Correction a: this supersedes {v[0].human_id} because it changed", CIT, scope=SCOPE,
               source_agent="p", supersession_report=first)
    svc.ingest(f"Correction b: this supersedes {v[1].human_id} because it changed", CIT, scope=SCOPE,
               source_agent="p", supersession_report=second)
    assert first["bound_hit"] == [] and first["prose_filed"] == [v[0].id]
    assert second["bound_hit"] == [v[1].id] and second["prose_filed"] == []


def test_the_mcp_tool_says_when_the_bound_kept_a_prose_correction_out(tmp_path, monkeypatch):
    try:
        from memorymaster.surfaces.mcp_server import ingest_claim, init_db
    except ImportError:
        pytest.skip("MCP not installed")
    monkeypatch.setattr(ig, "MAX_DECLARED_PER_AGENT_PER_DAY", 1)
    db, workspace = str(tmp_path / "mcp.db"), str(tmp_path)
    init_db(db=db, workspace=workspace)
    base = {"db": db, "workspace": workspace, "scope": SCOPE, "source_agent": "p"}
    olds = [ingest_claim(text=f"The service number {i} of the fleet listens on its documented port", **base)["claim"]
            for i in range(2)]
    first = ingest_claim(text=f"Correction a: this supersedes {olds[0]['human_id']} because it changed", **base)
    second = ingest_claim(text=f"Correction b: this supersedes {olds[1]['human_id']} because it changed", **base)
    assert not any("daily supersession bound" in w for w in first.get("warnings", []))
    assert any("daily supersession bound" in w for w in second.get("warnings", [])), second

