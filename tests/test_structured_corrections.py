"""T-2276: a claim that corrects an earlier one supersedes it, declared (``supersedes``) or named in its text.

Declared: ``ingest(supersedes=[...])`` files one steward proposal per target (same scope only), and the target stops
being served as current at once. Named in prose: the safety net files an operator-only proposal, anchored on an
explicit claim reference, never on topic. Neither path applies anything by itself.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from memorymaster.core import lifecycle
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.correction_text import Reference, find_corrected_references
from memorymaster.govern.ingest_governance import queue_prose_corrections
from memorymaster.govern.jobs.curation_drain import drain_proposals
from memorymaster.govern.steward import OPERATOR_ONLY_SOURCES, resolve_steward_proposal
from memorymaster.recall.retrieval import pending_supersession_ids

SCOPE = "project:t2276"
PROPOSAL = "steward_proposal:superseded_candidate"


@pytest.fixture()
def svc(tmp_path: Path) -> MemoryService:
    service = MemoryService(tmp_path / "corrections.db", workspace_root=tmp_path)
    service.init_db()
    return service


def _claim(svc: MemoryService, text: str, *, scope: str = SCOPE, confirm: bool = True, **kw):
    made = svc.ingest(text, [CitationInput(source="test://t2276", locator="x")], scope=scope,
                      source_agent=kw.pop("source_agent", "fixture"), **kw)
    if confirm:
        lifecycle.transition_claim(svc.store, made.id, "confirmed", reason="fixture", event_type="validator")
    return svc.store.get_claim(made.id, include_citations=False)


def _proposals(svc: MemoryService, claim_id: int):
    return [e for e in svc.list_events(claim_id=claim_id, event_type="policy_decision", limit=50)
            if e.details == PROPOSAL]


def _approve(svc: MemoryService, claim_id: int) -> None:
    result = resolve_steward_proposal(svc, action="approve", claim_id=claim_id, actor="operator")
    assert result.get("applied", True) is not False, result


def _get(svc: MemoryService, claim_id: int):
    return svc.store.get_claim(claim_id, include_citations=False)


# --- text detector: anchored on an explicit reference ---------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("CORRECTION: the port is 9443; supersedes mm-a3f8", [Reference("human_id", "mm-a3f8")]),
    ("This claim corrects mm-a3f8.1 and replaces claim 131998.",
     [Reference("human_id", "mm-a3f8.1"), Reference("claim_id", 131998)]),
    ("The earlier claim mm-a3f8 was wrong.", [Reference("human_id", "mm-a3f8")]),
    ("Corrige a mm-a3f8: el puerto es 9443", [Reference("human_id", "mm-a3f8")]),
    ("mm-a3f8 era incorrecto, reemplaza al claim #42", [Reference("human_id", "mm-a3f8"), Reference("claim_id", 42)]),
    ("mm-A3F8 is no longer valid", [Reference("human_id", "mm-a3f8")]),
    # not anchored on an id, no cue, or negated: nothing
    ("the earlier claim that the panel listens on port 8080 was wrong and this claim corrects it", []),
    ("See mm-a3f8 for the background", []),
    ("This does not supersede mm-a3f8", []),
    ("No corrige a mm-a3f8", []),
    ("Replaces the nginx 1.2 config with port 8080", []),
    ("supersedes issue #123", []),
])
def test_text_detector_needs_a_cue_and_an_explicit_reference(text, expected) -> None:
    assert find_corrected_references(text) == expected


# --- 1. structured channel ------------------------------------------------------------------------------------


def test_supersedes_by_human_id_files_a_proposal_and_hides_the_target_at_once(svc) -> None:
    old = _claim(svc, "The staging admin panel listens on port 8080")
    assert old.id not in pending_supersession_ids(svc)  # warms the 300 s cache: it must not keep the stale answer
    new = _claim(svc, "The staging admin panel listens on port 9443", supersedes=[old.human_id])

    (event,) = _proposals(svc, old.id)
    payload = json.loads(event.payload_json)
    assert payload["source"] == "direct-ingest" and payload["replaced_by_claim_id"] == new.id
    assert payload["apply_requested"] is False
    assert "explicit_supersession_intent" in {r["code"] for r in payload["reasons"]}
    assert _get(svc, old.id).status == "confirmed", "ingest files a proposal; it does not rewrite confirmed truth"
    assert old.id in pending_supersession_ids(svc), "the very next recall must already treat the target as disputed"
    assert new.id not in pending_supersession_ids(svc)


def test_supersedes_accepts_numeric_ids_and_a_single_value(svc) -> None:
    a = _claim(svc, "Backups run at 02:00")
    b = _claim(svc, "Backups run at 03:00", supersedes=a.id)
    c = _claim(svc, "Backups run at 04:00", supersedes=str(b.id))
    assert [len(_proposals(svc, x.id)) for x in (a, b, c)] == [1, 1, 0]


def test_approval_sets_both_pointers_a_to_b(svc) -> None:
    a = _claim(svc, "Deploys go through jenkins")
    b = _claim(svc, "Deploys go through argo", supersedes=[a.human_id])
    _approve(svc, a.id)
    assert (_get(svc, a.id).status, _get(svc, a.id).replaced_by_claim_id) == ("superseded", b.id)
    assert _get(svc, b.id).supersedes_claim_id == a.id


def test_chain_a_b_c(svc) -> None:
    a = _claim(svc, "The cache TTL is 60 seconds")
    b = _claim(svc, "The cache TTL is 120 seconds", supersedes=[a.human_id])
    c = _claim(svc, "The cache TTL is 300 seconds", supersedes=[b.human_id])
    _approve(svc, a.id)
    _approve(svc, b.id)
    a, b, c = (_get(svc, x.id) for x in (a, b, c))
    assert (a.status, a.replaced_by_claim_id, b.supersedes_claim_id) == ("superseded", b.id, a.id)
    assert (b.status, b.replaced_by_claim_id, c.supersedes_claim_id) == ("superseded", c.id, b.id)
    assert c.status == "confirmed" and c.replaced_by_claim_id is None


def test_n_to_one_every_predecessor_is_retired(svc) -> None:
    a1 = _claim(svc, "Service X runs on host alpha", subject="svc-x", predicate="runs-on", object_value="alpha")
    a2 = _claim(svc, "Service X is also reachable at beta", subject="svc-x", predicate="reachable-at")
    b = _claim(svc, "Service X now runs only on gamma", supersedes=[a1.human_id, a2.id])
    assert len(_proposals(svc, a1.id)) == len(_proposals(svc, a2.id)) == 1
    assert {a1.id, a2.id} <= pending_supersession_ids(svc, use_cache=False)
    _approve(svc, a1.id)
    _approve(svc, a2.id)
    for old in (a1, a2):
        retired = _get(svc, old.id)
        assert (retired.status, retired.replaced_by_claim_id) == ("superseded", b.id)
    assert _get(svc, b.id).supersedes_claim_id == a1.id  # single-valued column: the first predecessor
    assert any((link.source_id, link.target_id, link.link_type) == (b.id, a2.id, "supersedes")
               for link in svc.store.get_claim_links(b.id))


def test_n_to_one_link_failure_leaves_the_extra_predecessor_untouched(svc, monkeypatch) -> None:
    from memorymaster.govern.steward import retire_for_replacement

    a1 = _claim(svc, "Service Y runs on host alpha")
    a2 = _claim(svc, "Service Y is reachable at beta")
    b = _claim(svc, "Service Y now runs on gamma", supersedes=[a1.id, a2.id])
    _approve(svc, a1.id)

    def broken(*_args, **_kwargs):
        raise RuntimeError("link table unavailable")

    monkeypatch.setattr(svc.store, "add_claim_link", broken)
    with pytest.raises(RuntimeError):
        retire_for_replacement(svc.store, a2.id, b.id, "test")
    assert _get(svc, a2.id).status == "confirmed" and _get(svc, a2.id).replaced_by_claim_id is None


def test_revocation_without_replacement_is_a_retraction_claim_that_supersedes(svc) -> None:
    old = _claim(svc, "The legacy FTP endpoint is open for partners")
    retraction = _claim(svc, "The legacy FTP endpoint no longer exists; nothing replaces it", supersedes=[old.human_id])
    _approve(svc, old.id)
    assert _get(svc, old.id).status == "superseded" and _get(svc, old.id).replaced_by_claim_id == retraction.id
    assert _get(svc, retraction.id).status != "superseded"


@pytest.mark.parametrize("make_ref", [lambda c: c.human_id, lambda c: c.id], ids=["human_id", "numeric_id"])
def test_a_correction_of_a_claim_in_another_scope_is_refused(svc, make_ref) -> None:
    other = _claim(svc, "The vpn gateway is vpn1", scope="project:elsewhere")
    before = len(svc.list_claims(limit=1000, include_archived=True))
    with pytest.raises(ValueError, match="does not exist"):
        _claim(svc, "The vpn gateway is vpn2", supersedes=[make_ref(other)])
    assert _get(svc, other.id).status == "confirmed" and not _proposals(svc, other.id)
    assert len(svc.list_claims(limit=1000, include_archived=True)) == before, "a refused correction leaves no claim"


@pytest.mark.parametrize("bad", ["not-an-id", "mm-", 0, -3, True, "mm-ffff"])
def test_malformed_or_unknown_references_are_refused(svc, bad) -> None:
    with pytest.raises(ValueError):
        _claim(svc, "Some replacement", supersedes=[bad])


def test_a_retired_target_cannot_be_corrected_again(svc) -> None:
    a = _claim(svc, "The queue limit is 10")
    _claim(svc, "The queue limit is 20", supersedes=[a.human_id])
    _approve(svc, a.id)
    with pytest.raises(ValueError, match="not active"):
        _claim(svc, "The queue limit is 30", supersedes=[a.human_id])


# --- 2. text safety net ---------------------------------------------------------------------------------------


def test_text_naming_an_id_files_an_operator_only_proposal_not_a_supersession(svc) -> None:
    old = _claim(svc, "The staging admin panel listens on port 8080")
    assert old.id not in pending_supersession_ids(svc)
    new = _claim(svc, f"Correction: the panel listens on port 9443; this supersedes {old.human_id}")

    (event,) = _proposals(svc, old.id)
    payload = json.loads(event.payload_json)
    assert payload["source"] == "prose-correction" and payload["replaced_by_claim_id"] == new.id
    assert payload["apply_requested"] is False
    assert "prose-correction" in OPERATOR_ONLY_SOURCES
    assert _get(svc, old.id).status == "confirmed"
    assert old.id in pending_supersession_ids(svc)


def test_prose_proposals_are_not_applied_by_automation(svc) -> None:
    old = _claim(svc, "Retention is 30 days")
    _claim(svc, f"Retention is 90 days. Corrige a {old.human_id}.")
    summary = drain_proposals(svc, apply=True)
    assert summary["approved"] == 0 and summary["kept_for_operator"] >= 1
    assert _get(svc, old.id).status == "confirmed" and _get(svc, old.id).replaced_by_claim_id is None
    _approve(svc, old.id)  # the operator still can
    assert _get(svc, old.id).status == "superseded"


def test_same_agent_declared_proposals_stay_on_the_existing_drain_path(svc) -> None:
    old = _claim(svc, "Retention is 30 days")
    _claim(svc, "Retention is 90 days", supersedes=[old.human_id])
    assert drain_proposals(svc, apply=False)["approved"] == 1  # same source_agent: unchanged behaviour of direct-ingest
    assert drain_proposals(svc, apply=True)["approved"] == 1
    assert _get(svc, old.id).status == "superseded"


def test_a_declared_supersession_of_another_agents_claim_is_never_drain_applied(svc) -> None:
    old = _claim(svc, "Retention is 30 days", source_agent="agent-a")
    junk = _claim(svc, "junk", source_agent="agent-b", supersedes=[old.human_id])
    (event,) = _proposals(svc, old.id)
    payload = json.loads(event.payload_json)
    assert payload["cross_identity"] is True and payload["filed_by"] == "agent-b"
    assert payload["replaced_by_claim_id"] == junk.id

    summary = drain_proposals(svc, apply=True)
    assert summary["approved"] == 0 and summary["kept_for_operator"] >= 1
    assert _get(svc, old.id).status == "confirmed" and _get(svc, old.id).replaced_by_claim_id is None
    _approve(svc, old.id)  # the operator still can
    assert _get(svc, old.id).status == "superseded"


def test_declared_supersessions_are_bounded_per_agent_per_day(svc, monkeypatch) -> None:
    from memorymaster.govern import ingest_governance

    monkeypatch.setattr(ingest_governance, "MAX_DECLARED_PER_AGENT_PER_DAY", 3)
    olds = [_claim(svc, f"Fact number {n} about hosts", source_agent="agent-a") for n in range(6)]
    for old in olds[:3]:
        _claim(svc, f"Replacement for {old.id}", source_agent="agent-a", supersedes=[old.id])
    with pytest.raises(ValueError, match="supersedes limit reached"):
        _claim(svc, "one too many", source_agent="agent-a", supersedes=[olds[3].id])
    assert not _proposals(svc, olds[3].id)
    # another agent has its own budget
    _claim(svc, "Other agent replacement", source_agent="agent-b", supersedes=[olds[4].id])


def test_supersedes_accepts_at_most_five_targets_per_call(svc) -> None:
    olds = [_claim(svc, f"Distinct fact {n} about disks") for n in range(6)]
    with pytest.raises(ValueError, match="at most 5"):
        _claim(svc, "Replaces everything", supersedes=[o.id for o in olds])
    _claim(svc, "Replaces five", supersedes=[o.id for o in olds[:5]])


def test_a_rejected_proposal_does_not_block_the_real_correction(svc) -> None:
    target = _claim(svc, "The vault listens on 8200")
    junk_report: dict = {}
    _claim(svc, "junk claim", supersedes=[target.id], supersession_report=junk_report)
    assert junk_report["filed"] == [target.id]
    resolve_steward_proposal(svc, action="reject", claim_id=target.id, actor="operator")
    assert target.id not in pending_supersession_ids(svc, use_cache=False)

    real_report: dict = {}
    real = _claim(svc, "The vault listens on 8201", supersedes=[target.id], supersession_report=real_report)
    assert real_report["filed"] == [target.id] and real_report["not_filed"] == []
    live = [e for e in _proposals(svc, target.id) if json.loads(e.payload_json)["replaced_by_claim_id"] == real.id]
    assert len(live) == 1 and target.id in pending_supersession_ids(svc, use_cache=False)


def test_a_live_proposal_blocks_a_second_one_and_ingest_says_so(svc) -> None:
    target = _claim(svc, "The vault listens on 8200")
    _claim(svc, "first correction", supersedes=[target.id])
    report: dict = {}
    _claim(svc, "second correction", supersedes=[target.id], supersession_report=report)
    assert report["filed"] == [] and report["not_filed"] == [target.id]
    assert len(_proposals(svc, target.id)) == 1


def test_text_naming_a_claim_of_another_scope_files_nothing(svc) -> None:
    other = _claim(svc, "The vpn gateway is vpn1", scope="project:elsewhere")
    _claim(svc, f"The vpn gateway is vpn2 and supersedes {other.human_id}")
    _claim(svc, f"The vpn gateway is vpn3 and corrects claim {other.id}")
    assert not _proposals(svc, other.id)


def test_text_without_an_explicit_id_files_nothing(svc) -> None:
    old = _claim(svc, "The staging admin panel listens on port 8080")
    _claim(svc, "Correction: the staging admin panel listens on port 9443; the earlier claim that the staging admin "
                "panel listens on port 8080 was wrong and this claim corrects it")
    assert not _proposals(svc, old.id)


def test_text_naming_a_newer_claim_files_nothing(svc) -> None:
    first = _claim(svc, "Alpha is up")
    later = _claim(svc, "Beta is up")
    stale = dataclasses.replace(first, text=f"This corrects {later.human_id}")  # an older claim never corrects a newer one
    assert queue_prose_corrections(svc.store, stale, 0.5) == []
    assert not _proposals(svc, later.id)


def test_the_reproducer_pair_with_an_explicit_id_is_filed_once(svc) -> None:
    old = _claim(svc, "The staging admin panel listens on port 8080")
    _claim(svc, f"Port is 9443, supersedes {old.human_id}", supersedes=[old.human_id])
    assert len(_proposals(svc, old.id)) == 1, "declared + named for the same target is one proposal"


# --- MCP surface ---------------------------------------------------------------------------------------------


@pytest.fixture()
def mcp_db(tmp_path):
    try:
        from memorymaster.surfaces.mcp_server import init_db
    except ImportError:
        pytest.skip("MCP not installed")
    db, workspace = str(tmp_path / "mcp.db"), str(tmp_path)
    init_db(db=db, workspace=workspace)
    return db, workspace


def test_mcp_ingest_claim_accepts_supersedes(mcp_db) -> None:
    from memorymaster.surfaces.mcp_server import ingest_claim

    db, workspace = mcp_db
    base = {"db": db, "workspace": workspace, "scope": SCOPE, "source_agent": "pytest-t2276"}
    first = ingest_claim(text="The build runs on runner-a", **base)
    assert first["ok"], first
    hid = first["claim"]["human_id"]
    second = ingest_claim(text="The build runs on runner-b", supersedes=[hid, first["claim"]["id"]], **base)
    assert second["ok"], second
    assert "supersession queued for steward review" in second["warnings"]

    again = ingest_claim(text="The build runs on runner-d", supersedes=[hid], **base)
    assert again["ok"], again
    assert "supersession queued for steward review" not in again["warnings"]
    assert any("NOT queued" in w for w in again["warnings"]), again["warnings"]

    refused = ingest_claim(text="The build runs on runner-c", supersedes=["mm-ffff"], **base)
    assert refused["ok"] is False and refused["code"] == "VALIDATION_ERROR"


# --- 4. read-only sweep ---------------------------------------------------------------------------------------


def test_sweep_lists_unclosed_pairs_by_id_only_and_changes_nothing(svc, capsys) -> None:
    from scripts.find_prose_corrections import main, sweep

    old = _claim(svc, "Cert renewal is manual")
    new = _claim(svc, f"Cert renewal is automatic; supersedes {old.human_id}")
    done_old = _claim(svc, "Logs rotate weekly")
    _claim(svc, "Logs rotate daily", supersedes=[done_old.human_id])
    _approve(svc, done_old.id)  # pointers set: not listed
    _claim(svc, "Mentions mm-ffff without any cue")
    elsewhere = _claim(svc, "Other scope fact", scope="project:elsewhere")
    _claim(svc, f"corrects {elsewhere.human_id}")  # other scope: not listed
    db = str(svc.store.db_path)
    def state():  # file bytes move with WAL checkpoints of the fixture's own connections; compare content instead
        with svc.store.connect() as conn:
            return ([tuple(r) for r in conn.execute("SELECT id, status, supersedes_claim_id, replaced_by_claim_id FROM claims ORDER BY id")],
                    conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    before = state()

    assert sweep(db)["pairs"] == [(old.id, new.id, SCOPE)]
    assert main(["--db", db]) == 0
    out = capsys.readouterr().out
    assert f"{old.id}\t{new.id}\t{SCOPE}" in out and "uncorrected_pairs=1" in out
    assert "Cert renewal" not in out, "ids only, never claim text"
    assert state() == before, "the sweep applies nothing"
