"""S4 DEDUP (4.9.0): Jev judges candidate_dedupe's FTS pairs and only files proposals.

Contract (.planning/JEV-LIVE-4.9.0.md): per pair ``memory.same_fact`` (3-level
score), ``memory.contradicts``, ``memory.supersedes`` in two paraphrases and
``memory.same_scope``.  The live action is a steward proposal tagged
``source: jev`` with evidence and the decision id -- never a status change, never
a ranking demotion before a human approves, never auto-approved by curation_drain.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from _jev_fakes import ScriptedTransport, make_engine
from memorymaster.core import lifecycle
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.decisions import outcomes as oc
from memorymaster.decisions.ledger import DecisionLedger
from memorymaster.govern import candidate_dedupe
from memorymaster.govern.jobs import curation_drain
from memorymaster.govern.steward import is_jev_proposal, list_steward_proposals, resolve_steward_proposal
from memorymaster.recall import qdrant_outbox
from memorymaster.recall.retrieval import pending_supersession_ids

LOW = {"memory.contradicts": 0.05, "memory.supersedes": 0.1, "memory.supersedes_alt": 0.1}
DUPLICATE = {**LOW, "memory.same_fact": {"1": 0.05, "2": 0.05, "3": 0.9}, "memory.same_scope": 0.95}
CONFLICT = {**LOW, "memory.same_fact": {"1": 0.8, "2": 0.15, "3": 0.05}, "memory.contradicts": 0.95,
            "memory.same_scope": 0.95}
SUPERSEDE = {"memory.same_fact": {"1": 0.2, "2": 0.7, "3": 0.1}, "memory.contradicts": 0.6,
             "memory.supersedes": 0.9, "memory.supersedes_alt": 0.9, "memory.same_scope": 0.95}


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(qdrant_outbox.ENV_OUTBOX_DIR, str(tmp_path / "qdrant-outbox"))
    monkeypatch.delenv("QDRANT_URL", raising=False)
    ledger = tmp_path / "decisions.db"
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(ledger))
    service = MemoryService(tmp_path / "s4.db", workspace_root=tmp_path)
    service.init_db()
    return service, ledger


def _claim(service: MemoryService, text: str, *, status: str = "candidate", **fields):
    claim = service.ingest(text, [CitationInput(source="test://s4")], scope="project:mm")
    if status == "confirmed":
        lifecycle.transition_claim(service.store, claim.id, "confirmed", reason="fixture", event_type="validator")
    if fields:
        with service.store.connect() as conn:
            conn.execute(f"UPDATE claims SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                         (*fields.values(), claim.id))
            conn.commit()
    return service.store.get_claim(claim.id, include_citations=False)


def _pair(service: MemoryService):
    existing = _claim(service, "Postgres for the billing service listens on port 5432", status="confirmed")
    candidate = _claim(service, "Postgres for the billing service listens on port 5433")
    return existing, candidate


def _jev_proposals(service: MemoryService) -> list[dict]:
    return [p for p in list_steward_proposals(service, limit=100, include_resolved=True)
            if is_jev_proposal(p.get("payload"))]


def _status(service: MemoryService, claim) -> str:
    return service.store.get_claim(claim.id, include_citations=False).status


def _dedup_decisions(ledger: Path) -> list[tuple]:
    with sqlite3.connect(ledger) as conn:
        return conn.execute("SELECT decision_id, mode, fallback_reason, action_taken FROM decisions "
                            "WHERE surface='dedup' ORDER BY ts").fetchall()


def test_a_duplicate_pair_files_a_jev_proposal_and_changes_no_status(env):
    service, ledger = env
    existing, candidate = _pair(service)

    result = candidate_dedupe.run_jev(service.store, engine=make_engine(ledger, ScriptedTransport(DUPLICATE)))

    assert result["asked"] == 1 and result["proposals"] == 1, result
    [(decision_id, mode, fallback, action)] = _dedup_decisions(ledger)
    assert (mode, fallback, action) == ("live", None, '"propose_duplicate"')
    [proposal] = _jev_proposals(service)
    payload = proposal["payload"]
    assert proposal["claim_id"] == candidate.id and proposal["status"] == "pending"
    assert (payload["source"], payload["decision"], payload["proposed_status"]) == (
        "jev", "superseded_candidate", "superseded")
    assert payload["replaced_by_claim_id"] == existing.id
    assert payload["decision_id"] == decision_id
    assert payload["evidence"]["same_fact"] == {"1": 0.05, "2": 0.05, "3": 0.9}
    assert payload["evidence"]["same_scope"] == pytest.approx(0.95)
    assert (_status(service, existing), _status(service, candidate)) == ("confirmed", "candidate")
    # A proposal is not a live degrade: recall and the validator ignore it until approved.
    assert candidate.id not in pending_supersession_ids(service.store, use_cache=False)
    [event] = [e for e in service.store.list_events(claim_id=candidate.id, event_type="policy_decision")]
    assert event.details == "steward_proposal:jev_superseded_candidate"


def test_curation_drain_never_auto_approves_a_proposal_written_by_s4(env):
    service, ledger = env
    existing, candidate = _pair(service)
    candidate_dedupe.run_jev(service.store, engine=make_engine(ledger, ScriptedTransport(DUPLICATE)))

    drain = curation_drain.drain_proposals(service, apply=True)

    assert drain["kept_jev"] == 1 and drain["approved"] == 0, drain
    [proposal] = _jev_proposals(service)
    assert proposal["status"] == "pending"
    assert (_status(service, existing), _status(service, candidate)) == ("confirmed", "candidate")


def test_an_operator_approval_applies_the_proposal_and_joins_as_a_labelled_outcome(env):
    service, ledger = env
    existing, candidate = _pair(service)
    candidate_dedupe.run_jev(service.store, engine=make_engine(ledger, ScriptedTransport(DUPLICATE)))
    [proposal] = _jev_proposals(service)

    resolve_steward_proposal(service, action="approve", proposal_event_id=proposal["proposal_event_id"],
                             actor="operator")
    oc.tail_lifecycle(DecisionLedger(ledger), service.store.db_path)

    after = service.store.get_claim(candidate.id, include_citations=False)
    assert (after.status, after.replaced_by_claim_id) == ("superseded", existing.id)
    labels = {(r["item_ref"], r["kind"], r["label_source"])
              for r in DecisionLedger(ledger).query("SELECT item_ref, kind, label_source FROM outcomes")}
    assert (f"claim:{candidate.id}", "proposal_approved", "operator") in labels


def test_supersession_needs_both_paraphrases_and_targets_the_older_claim(env):
    service, ledger = env
    existing, candidate = _pair(service)

    candidate_dedupe.run_jev(service.store, engine=make_engine(ledger, ScriptedTransport(SUPERSEDE)))

    [proposal] = _jev_proposals(service)
    assert proposal["claim_id"] == existing.id
    assert proposal["payload"]["decision"] == "superseded_candidate"
    assert proposal["payload"]["replaced_by_claim_id"] == candidate.id
    assert _status(service, existing) == "confirmed"


def test_one_supersession_paraphrase_alone_files_nothing(env):
    service, ledger = env
    _pair(service)

    result = candidate_dedupe.run_jev(service.store, engine=make_engine(
        ledger, ScriptedTransport({**SUPERSEDE, "memory.supersedes_alt": 0.3})))

    assert result["proposals"] == 0 and _jev_proposals(service) == []


def test_a_contradiction_files_a_conflicted_proposal(env):
    service, ledger = env
    existing, candidate = _pair(service)

    candidate_dedupe.run_jev(service.store, engine=make_engine(ledger, ScriptedTransport(CONFLICT)))

    [proposal] = _jev_proposals(service)
    assert proposal["claim_id"] == candidate.id
    assert (proposal["payload"]["decision"], proposal["payload"]["proposed_status"]) == ("conflicted", "conflicted")
    assert proposal["payload"]["related_claim_id"] == existing.id
    assert _status(service, candidate) == "candidate"


def test_different_scopes_file_nothing(env):
    service, ledger = env
    _pair(service)

    result = candidate_dedupe.run_jev(service.store, engine=make_engine(
        ledger, ScriptedTransport({**DUPLICATE, "memory.same_scope": 0.2})))

    assert result["proposals"] == 0 and _jev_proposals(service) == []


def test_shadow_mode_logs_the_would_be_proposal_only(env):
    service, ledger = env
    _pair(service)

    result = candidate_dedupe.run_jev(service.store, engine=make_engine(ledger, ScriptedTransport(DUPLICATE),
                                                                    mode="shadow"))

    assert result["would_propose"] == 1 and result["proposals"] == 0
    assert _jev_proposals(service) == []
    assert [row[1] for row in _dedup_decisions(ledger)] == ["shadow"]


def test_off_mode_sends_nothing_and_creates_no_ledger(env):
    service, ledger = env
    _pair(service)
    transport = ScriptedTransport(DUPLICATE)

    result = candidate_dedupe.run_jev(service.store, engine=make_engine(ledger, transport, mode="off"))

    assert result["stopped"] == "mode_off" and transport.calls == []
    assert not ledger.exists()


def test_run_cycle_output_is_unchanged_while_jev_is_off(env):
    service, ledger = env
    _pair(service)

    result = service.run_cycle()

    assert "jev" not in result["dedupe"], result["dedupe"]
    assert not ledger.exists()


LEGACY_KEYS = {"enabled", "shadow", "archived", "would_archive", "passthrough", "avg_jaccard", "results"}


@pytest.mark.parametrize("mode", ["live", "shadow"])
def test_run_cycle_never_asks_jev_whatever_the_mode(env, monkeypatch, mode):
    # S4 lives outside run_cycle (MCP run_cycle, CLI run-cycle, the per-turn
    # operator cycle and the scheduler all call it): only the steward-cycle hook
    # calls run_jev, so run_cycle sends nothing and writes no ledger row.
    from memorymaster.decisions import engine as decisions_engine

    service, ledger = env
    _pair(service)
    transport = ScriptedTransport(DUPLICATE)
    engine = make_engine(ledger, transport, mode=mode)
    monkeypatch.setenv("MEMORYMASTER_JEV_MODE", mode)
    monkeypatch.setattr(decisions_engine, "default_engine", lambda: engine)

    result = service.run_cycle()

    assert set(result["dedupe"]) == LEGACY_KEYS, result["dedupe"]
    assert transport.calls == []
    assert not ledger.exists() or DecisionLedger(ledger).query("SELECT decision_id FROM decisions") == []
    assert _jev_proposals(service) == []


@pytest.mark.parametrize("mode", ["off", "live", "shadow"])
def test_the_legacy_stage_output_is_identical_in_every_jev_mode(env, monkeypatch, mode):
    from memorymaster.decisions import engine as decisions_engine

    service, ledger = env
    _pair(service)
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_ENABLED", "1")
    transport = ScriptedTransport(DUPLICATE)
    monkeypatch.setenv("MEMORYMASTER_JEV_MODE", mode)
    monkeypatch.setattr(decisions_engine, "default_engine", lambda: make_engine(ledger, transport, mode=mode))

    result = candidate_dedupe.run(service.store)

    assert set(result) == LEGACY_KEYS and transport.calls == []


def test_an_answered_pair_is_not_asked_again_next_cycle(env):
    service, ledger = env
    _pair(service)
    engine = make_engine(ledger, ScriptedTransport(DUPLICATE))
    candidate_dedupe.run_jev(service.store, engine=engine)

    again = candidate_dedupe.run_jev(service.store, engine=engine)

    assert again["asked"] == 0 and again["skipped_already_asked"] == 1, again
    assert len(_jev_proposals(service)) == 1


def test_the_per_cycle_cap_bounds_the_requests(env, monkeypatch):
    service, ledger = env
    _pair(service)
    _claim(service, "Redis for the billing service listens on port 6379", status="confirmed")
    _claim(service, "Redis for the billing service listens on port 6380")
    monkeypatch.setenv(candidate_dedupe.JEV_PAIRS_PER_CYCLE_ENV, "1")
    transport = ScriptedTransport(DUPLICATE)

    result = candidate_dedupe.run_jev(service.store, engine=make_engine(ledger, transport))

    assert result["asked"] == 1 and len(transport.calls) == 1, result


def test_pairs_across_tenants_or_with_private_claims_are_never_sent(env):
    service, ledger = env
    _claim(service, "Postgres for the billing service listens on port 5432", status="confirmed", tenant_id="globex")
    _claim(service, "Postgres for the billing service listens on port 5433")
    _claim(service, "Kafka broker retention is seven days", status="confirmed", visibility="sensitive")
    _claim(service, "Kafka broker retention is eight days")
    transport = ScriptedTransport(DUPLICATE)

    result = candidate_dedupe.run_jev(service.store, engine=make_engine(ledger, transport))

    assert result["asked"] == 0 and transport.calls == []


def test_a_candidate_the_legacy_stage_archived_is_not_asked_about(env, monkeypatch):
    service, ledger = env
    _claim(service, "Postgres for the billing service listens on port 5432", status="confirmed")
    duplicate = _claim(service, "Postgres for the billing service listens on port 5432 today")
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_ENABLED", "1")
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_SHADOW", "0")
    transport = ScriptedTransport(DUPLICATE)

    legacy = candidate_dedupe.run(service.store)  # inside run_cycle, before the hook calls run_jev
    result = candidate_dedupe.run_jev(service.store, engine=make_engine(ledger, transport))

    assert legacy["archived"] == 1 and _status(service, duplicate) == "archived"
    assert result["asked"] == 0 and transport.calls == []


def test_a_pair_judged_in_shadow_is_asked_again_once_live(env):
    service, ledger = env
    _pair(service)
    transport = ScriptedTransport(DUPLICATE)
    candidate_dedupe.run_jev(service.store, engine=make_engine(ledger, transport, mode="shadow"))

    live = candidate_dedupe.run_jev(service.store, engine=make_engine(ledger, transport))

    assert live["asked"] == 1 and live["proposals"] == 1, live


def test_a_proposal_the_store_refuses_leaves_an_apply_failed_outcome(env):
    # The decision row says propose_duplicate; when the claims changed while Jev
    # answered, no proposal is filed and the ledger must say the action never happened.
    service, ledger = env
    existing, candidate = _pair(service)

    def archive_existing(_payload):  # another process retires the existing claim meanwhile
        conn = sqlite3.connect(service.store.db_path)
        try:
            conn.execute("UPDATE claims SET status='archived', version=version+1 WHERE id=?", (existing.id,))
            conn.commit()
        finally:
            conn.close()

    transport = ScriptedTransport(DUPLICATE, on_send=archive_existing)
    result = candidate_dedupe.run_jev(service.store, engine=make_engine(ledger, transport))

    assert result["asked"] == 1 and result["proposals"] == 0, result
    assert result["apply_failed"] == 1 and _jev_proposals(service) == []
    [(decision_id, _mode, _fallback, action)] = _dedup_decisions(ledger)
    assert action == '"propose_duplicate"'
    [row] = DecisionLedger(ledger).query(
        "SELECT decision_id, item_ref, details_json FROM outcomes WHERE kind = 'apply_failed'")
    assert row["decision_id"] == decision_id and row["item_ref"] == f"pair:{existing.id}-{candidate.id}"
    assert '"claim_status_changed"' in row["details_json"]


def test_pairs_with_a_sensitive_claim_are_never_sent(env):
    # Mirrors recall: a public claim the sensitivity scan flags never leaves the
    # machine, whether it is the candidate or the FTS-matched existing claim.
    service, ledger = env
    secret_existing = _claim(service, "Kafka broker retention is seven days on the billing cluster",
                             status="confirmed")
    _claim(service, "Kafka broker retention is eight days on the billing cluster")
    _claim(service, "Redis cache for the billing service listens on port 6379", status="confirmed")
    secret_candidate = _claim(service, "Redis cache for the billing service listens on port 6380")
    with service.store.connect() as conn:  # disposable fixture: stored redaction markers
        for claim in (secret_existing, secret_candidate):
            conn.execute("UPDATE claims SET text=? WHERE id=?",
                         (claim.text + " SECRETMARKER [REDACTED:password]", claim.id))
        conn.commit()
    existing, candidate = _pair(service)
    transport = ScriptedTransport(DUPLICATE)

    summary = candidate_dedupe.jev_review(service.store, engine=make_engine(ledger, transport))

    assert summary["asked"] >= 1 and transport.calls, summary
    assert "SECRETMARKER" not in transport.sent_text()
    refs = {row["item_ref"] for row in DecisionLedger(ledger).query("SELECT item_ref FROM decision_items")}
    assert f"pair:{existing.id}-{candidate.id}" in refs
    assert not refs & {f"claim:{secret_existing.id}", f"claim:{secret_candidate.id}"}, refs


# ------------------------------------------ who may resolve a Jev proposal ---

def _jev_proposal(service, ledger):
    existing, candidate = _pair(service)
    candidate_dedupe.run_jev(service.store, engine=make_engine(ledger, ScriptedTransport(DUPLICATE)))
    [proposal] = _jev_proposals(service)
    return existing, candidate, proposal


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_mcp_cannot_resolve_a_jev_proposal(env, action):
    # A model judgment waits for the operator: an MCP client (an agent) must not
    # turn it into a live action, nor dismiss it.
    from memorymaster.surfaces.mcp_server import resolve_steward_proposal as mcp_resolve

    service, ledger = env
    _existing, candidate, proposal = _jev_proposal(service, ledger)

    out = mcp_resolve(action=action, db=str(service.store.db_path), workspace=str(service.workspace_root),
                      proposal_event_id=proposal["proposal_event_id"])

    assert out["ok"] is False and out["code"] == "JEV_PROPOSAL_OPERATOR_ONLY", out
    [after] = _jev_proposals(service)
    assert after["status"] == "pending" and _status(service, candidate) == "candidate"
    out = mcp_resolve(action=action, db=str(service.store.db_path), workspace=str(service.workspace_root),
                      claim_id=candidate.id)
    assert out["ok"] is False and out["code"] == "JEV_PROPOSAL_OPERATOR_ONLY", out


def test_mcp_still_resolves_a_non_jev_proposal(env):
    from memorymaster.surfaces.mcp_server import resolve_steward_proposal as mcp_resolve

    service, _ledger = env
    old = _claim(service, "The nightly backup job starts at two in the morning", status="confirmed")
    service.ingest("The nightly backup job starts at three in the morning", [CitationInput(source="test://s4")],
                   scope="project:mm", supersedes_claim_id=old.id)
    [proposal] = [p for p in list_steward_proposals(service, limit=100) if p["claim_id"] == old.id]

    out = mcp_resolve(action="reject", db=str(service.store.db_path), workspace=str(service.workspace_root),
                      proposal_event_id=proposal["proposal_event_id"])

    assert out["ok"] is True and out["result"]["status"] == "rejected", out


def test_the_cli_resolves_a_jev_proposal_only_with_an_explicit_operator_actor(env, capsys):
    from memorymaster.surfaces.cli import main

    service, ledger = env
    existing, candidate, proposal = _jev_proposal(service, ledger)
    base = ["--db", str(service.store.db_path), "resolve-proposal", "--action", "approve",
            "--proposal-event-id", str(proposal["proposal_event_id"])]

    assert main(base) != 0
    assert main([*base, "--actor", "automation"]) != 0
    assert _jev_proposals(service)[0]["status"] == "pending" and _status(service, candidate) == "candidate"
    capsys.readouterr()

    assert main([*base, "--actor", "operator"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "approved", result
    assert _status(service, candidate) == "superseded"
