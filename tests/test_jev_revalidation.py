"""S1 REVALIDATE (4.9.0): Jev re-confirms stale claims and judges the useless ones.

Contract (.planning/JEV-LIVE-4.9.0.md): one request per stale claim, ages passed
as named buckets; ``still_valid`` and ``useful_future`` above their accept
thresholds re-confirm through the lifecycle (event ``validator``, details
``jev_revalidation:<decision_id>``, confidence = validator promote score); both
below ``low`` record the ``no_longer_useful`` judgment the archive gate reads;
anything else stays stale.  S1 never archives, fallbacks never act, and the job
stops cleanly on budget/breaker exhaustion.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from _jev_fakes import COST, ScriptedTransport, make_engine
from memorymaster.core.config import get_config
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.decisions import archive_gate
from memorymaster.decisions import outcomes as oc
from memorymaster.decisions.ledger import DecisionLedger
from memorymaster.decisions.questions import AGE_BUCKETS
from memorymaster.govern.jobs import integrity, revalidation, scheduled_archive
from memorymaster.govern.jobs.validator import validation_score
from memorymaster.govern.steward import resolve_steward_proposal
from memorymaster.recall import qdrant_outbox
from memorymaster.recall.retrieval import pending_supersession_ids

KEEP = {"lifecycle.still_valid": 0.5, "lifecycle.durable": 0.5, "lifecycle.useful_future": 0.5}
CONFIRM = {"lifecycle.still_valid": 0.95, "lifecycle.durable": 0.9, "lifecycle.useful_future": 0.9}
USELESS = {"lifecycle.still_valid": 0.05, "lifecycle.durable": 0.5, "lifecycle.useful_future": 0.1}


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(qdrant_outbox.ENV_OUTBOX_DIR, str(tmp_path / "qdrant-outbox"))
    monkeypatch.delenv("QDRANT_URL", raising=False)
    ledger = tmp_path / "decisions.db"
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(ledger))
    service = MemoryService(tmp_path / "s1.db", workspace_root=tmp_path)
    service.init_db()
    return service, ledger


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(microsecond=0).isoformat()


def _stale(service: MemoryService, text: str, *, days_ago: float = 30.0, **fields):
    claim = service.ingest(text, [CitationInput(source="test://s1")], confidence=0.4, scope="project:mm")
    old = _iso(days_ago)
    # A stale row written directly: no fixture transition events, so the lifecycle
    # tail sees only what S1 itself does (the events table is append-only).
    sets = {"status": "stale", "created_at": old, "updated_at": old, "access_count": 0, **fields}
    with service.store.connect() as conn:
        conn.execute(f"UPDATE claims SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), claim.id))
        conn.commit()
    return service.store.get_claim(claim.id, include_citations=False)


def _decisions(ledger: Path) -> list[tuple]:
    with sqlite3.connect(ledger) as conn:
        return conn.execute(
            "SELECT d.decision_id, d.surface, d.mode, d.fallback_reason, d.action_taken, i.item_ref "
            "FROM decisions d JOIN decision_items i USING (decision_id) GROUP BY d.decision_id, i.item_ref "
            "ORDER BY d.ts"
        ).fetchall()


def test_still_valid_and_useful_claim_is_reconfirmed_through_the_lifecycle(env):
    service, ledger = env
    claim = _stale(service, "The steward cycle runs the validator job before decay in MemoryMaster")
    citations = service.store.count_citations(claim.id)
    expected_score = validation_score(claim, citations, prior_confidence=claim.confidence)
    transport = ScriptedTransport(CONFIRM)

    summary = revalidation.run(service, engine=make_engine(ledger, transport))

    assert summary["asked"] == 1 and summary["reconfirmed"] == 1, summary
    after = service.store.get_claim(claim.id, include_citations=False)
    assert after.status == "confirmed"
    assert after.confidence == pytest.approx(expected_score)
    [(decision_id, surface, mode, fallback, action, ref)] = _decisions(ledger)
    assert (surface, mode, fallback, action, ref) == ("revalidate", "live", None, '"reconfirm"', f"claim:{claim.id}")
    [event] = [e for e in service.store.list_events(claim_id=claim.id, limit=50)
               if e.from_status == "stale" and e.to_status == "confirmed"]
    assert event.event_type == "validator"
    assert event.details == f"jev_revalidation:{decision_id}"


def test_both_answers_below_low_record_the_judgment_the_archive_gate_reads(env):
    service, ledger = env
    claim = _stale(service, "Temporary note about a flaky CI run on an old branch")

    summary = revalidation.run(service, engine=make_engine(ledger, ScriptedTransport(USELESS)))

    assert summary["no_longer_useful"] == 1 and summary["reconfirmed"] == 0, summary
    current = service.store.get_claim(claim.id, include_citations=False)
    assert current.status == "stale", "S1 never archives by itself"
    assert archive_gate.has_no_longer_useful_judgment(claim.id, not_before=current.updated_at) is True
    # The judgment is what lets the (separate) scheduled archive act.
    assert scheduled_archive.run(service, older_than_days=14)["archived"] == 1


@pytest.mark.parametrize("answers", [
    KEEP,
    {**CONFIRM, "lifecycle.useful_future": 0.5},   # valid but not useful enough
    {**USELESS, "lifecycle.useful_future": 0.5},   # only one answer below low
])
def test_anything_else_stays_stale_without_a_judgment(env, answers):
    service, ledger = env
    claim = _stale(service, "Some stale statement about the recall ranking weights")

    summary = revalidation.run(service, engine=make_engine(ledger, ScriptedTransport(answers)))

    assert summary["kept_stale"] == 1, summary
    assert service.store.get_claim(claim.id, include_citations=False).status == "stale"
    assert archive_gate.has_no_longer_useful_judgment(claim.id) is False


def test_shadow_mode_logs_what_jev_would_do_and_changes_nothing(env):
    service, ledger = env
    confirm = _stale(service, "Claim Jev would re-confirm in live mode about WAL checkpoints")
    useless = _stale(service, "Claim Jev would judge useless about a one-off typo fix")
    transport = ScriptedTransport(per_ref={f"claim:{confirm.id}": CONFIRM, f"claim:{useless.id}": USELESS})

    summary = revalidation.run(service, engine=make_engine(ledger, transport, mode="shadow"), concurrency=1)

    assert (summary["would_reconfirm"], summary["would_judge_no_longer_useful"]) == (1, 1), summary
    assert summary["reconfirmed"] == summary["no_longer_useful"] == 0
    assert {service.store.get_claim(c.id).status for c in (confirm, useless)} == {"stale"}
    assert archive_gate.has_no_longer_useful_judgment(useless.id) is False
    assert {row[2] for row in _decisions(ledger)} == {"shadow"}


def test_off_mode_sends_nothing_and_creates_no_ledger(env):
    service, ledger = env
    _stale(service, "A stale claim while Jev is off")
    transport = ScriptedTransport(CONFIRM)

    summary = revalidation.run(service, engine=make_engine(ledger, transport, mode="off"))

    assert summary["stopped"] == "mode_off" and summary["asked"] == 0
    assert transport.calls == []
    assert not ledger.exists()


def test_failures_fall_back_and_the_open_breaker_stops_the_job(env):
    service, ledger = env
    claims = [_stale(service, f"Stale claim number {n} about the hook deadlines") for n in range(8)]
    transport = ScriptedTransport(CONFIRM, outcome="network_error")

    summary = revalidation.run(service, engine=make_engine(ledger, transport), concurrency=1)

    assert summary["stopped"] == "breaker_open", summary
    assert summary["fallbacks"]["network_error"] == 5
    assert len(transport.calls) == 5  # open breaker: no probe within 60 s of the last request; the job stops
    assert {service.store.get_claim(c.id).status for c in claims} == {"stale"}


def test_exhausted_daily_budget_stops_before_any_request(env):
    service, ledger = env
    claim = _stale(service, "A stale claim when the daily Jev budget is spent")
    transport = ScriptedTransport(CONFIRM)
    engine = make_engine(ledger, transport, env={"MEMORYMASTER_JEV_DAILY_USD_CAP": "0.000000001"})

    summary = revalidation.run(service, engine=engine)

    assert summary["stopped"] == "budget_exhausted" and transport.calls == []
    assert service.store.get_claim(claim.id).status == "stale"


def test_run_usd_cap_stops_cleanly(env):
    service, ledger = env
    for n in range(5):
        _stale(service, f"Stale claim {n} for the per-run spending cap")
    transport = ScriptedTransport(KEEP)

    summary = revalidation.run(service, engine=make_engine(ledger, transport), max_usd=COST * 2.5, concurrency=1)

    assert summary["stopped"] == "max_usd", summary
    assert summary["asked"] == 2 and summary["spent_usd"] == pytest.approx(COST * 2)


def test_oldest_first_limit_and_no_reasking_of_judged_claims(env):
    service, ledger = env
    newest = _stale(service, "Newest stale claim about recall", days_ago=10)
    oldest = _stale(service, "Oldest stale claim about ingest", days_ago=90)
    middle = _stale(service, "Middle stale claim about decay", days_ago=40)
    transport = ScriptedTransport(KEEP)
    engine = make_engine(ledger, transport)

    first = revalidation.run(service, engine=engine, limit=2, concurrency=1)
    asked_first = [row[5] for row in _decisions(ledger)]
    second = revalidation.run(service, engine=engine, limit=2, concurrency=1)

    assert asked_first == [f"claim:{oldest.id}", f"claim:{middle.id}"]
    assert first["asked"] == 2 and second["asked"] == 1, (first, second)
    assert second["skipped_recently_judged"] == 2
    assert [row[5] for row in _decisions(ledger)][-1] == f"claim:{newest.id}"


def test_backfill_walks_the_whole_stale_set_in_pages(env):
    service, ledger = env
    claims = [_stale(service, f"Backlog stale claim {n} about scopes", days_ago=50 + n) for n in range(5)]
    transport = ScriptedTransport(KEEP)

    summary = revalidation.run(service, engine=make_engine(ledger, transport), limit=2, backfill=True)

    assert summary["asked"] == 5, summary
    assert {row[5] for row in _decisions(ledger)} == {f"claim:{c.id}" for c in claims}


def test_only_redacted_text_and_named_age_buckets_leave(env):
    service, ledger = env
    claim = _stale(service, "Build notes for the release pipeline", days_ago=45)
    with service.store.connect() as conn:  # a legacy row that predates the ingest path filters
        conn.execute("UPDATE claims SET text=? WHERE id=?",
                     ("Build notes live in /mnt/c/Users/alice/repo; ask alice@example.org", claim.id))
        conn.commit()
    transport = ScriptedTransport(KEEP)

    revalidation.run(service, engine=make_engine(ledger, transport))

    sent = transport.sent_text()
    assert "alice" not in sent.lower()
    [payload] = transport.calls
    assert payload["state"]["claim"]["age"] == "30_to_90_days" and payload["state"]["claim"]["age"] in AGE_BUCKETS
    assert claim.created_at[:10] not in sent, "dates never reach Jev; only named buckets"


def test_non_public_and_other_tenant_claims_are_never_sent(env):
    service, ledger = env
    private = _stale(service, "Private stale note about the operator", visibility="sensitive")
    foreign = _stale(service, "Another tenant's stale note", tenant_id="globex")
    transport = ScriptedTransport(CONFIRM)

    summary = revalidation.run(service, engine=make_engine(ledger, transport))

    assert summary["asked"] == 0 and transport.calls == []
    assert {service.store.get_claim(c.id).status for c in (private, foreign)} == {"stale"}


def test_a_claim_edited_while_jev_was_answering_is_not_reconfirmed(env):
    service, ledger = env
    claim = _stale(service, "Stale claim that someone edits during the request")

    def edit(_payload):
        conn = sqlite3.connect(service.store.db_path)
        try:
            conn.execute("UPDATE claims SET text=?, version=version+1 WHERE id=?", ("Edited text", claim.id))
            conn.commit()
        finally:
            conn.close()

    summary = revalidation.run(service, engine=make_engine(ledger, ScriptedTransport(CONFIRM, on_send=edit)))

    assert summary["changed_since_asked"] == 1 and summary["reconfirmed"] == 0, summary
    assert service.store.get_claim(claim.id).status == "stale"
    # The decision row says "reconfirm"; the outcome says it never happened.
    [row] = _outcomes(ledger, "apply_failed")
    assert row["item_ref"] == f"claim:{claim.id}" and '"changed_since_asked"' in row["details_json"]


def test_reconfirmation_joins_as_one_revalidated_outcome_labelled_jev(env):
    service, ledger = env
    claim = _stale(service, "The operational review checks the decisions ledger freshness")
    revalidation.run(service, engine=make_engine(ledger, ScriptedTransport(CONFIRM)))

    oc.tail_lifecycle(DecisionLedger(ledger), service.store.db_path)

    rows = DecisionLedger(ledger).query("SELECT item_ref, kind, label_source FROM outcomes")
    # The confidence write that follows the transition is not a second outcome.
    assert [(r["item_ref"], r["kind"], r["label_source"]) for r in rows] == [
        (f"claim:{claim.id}", "revalidated", "jev")]


def test_postgres_store_fails_closed_without_asking(tmp_path):
    transport = ScriptedTransport(CONFIRM)
    service = SimpleNamespace(store=SimpleNamespace(dsn="postgresql://example/mm"), tenant_id=None)
    engine = make_engine(tmp_path / "decisions.db", transport)

    summary = revalidation.run(service, engine=engine)

    assert summary["ok"] is False and summary["stopped"] == "unsupported_store"
    assert transport.calls == []
    assert not (tmp_path / "decisions.db").exists()


@pytest.mark.parametrize("raw,expected", [
    (None, 500), ("25", 25), ("0", 0), ("abc", 500), ("-3", 500), (" 40 ", 40),
])
def test_per_cycle_cap_comes_from_the_environment(raw, expected):
    environ = {} if raw is None else {"MEMORYMASTER_JEV_REVALIDATE_PER_CYCLE": raw}
    assert revalidation.per_cycle_limit(environ) == expected


def test_zero_limit_does_nothing(env):
    service, ledger = env
    _stale(service, "A stale claim while the per-cycle cap is zero")
    transport = ScriptedTransport(CONFIRM)

    summary = revalidation.run(service, engine=make_engine(ledger, transport), limit=0)

    assert summary["asked"] == 0 and transport.calls == []


def test_a_claim_judged_in_shadow_is_asked_again_once_live(env):
    service, ledger = env
    claim = _stale(service, "Stale claim first judged while S1 ran in shadow")
    transport = ScriptedTransport(CONFIRM)
    revalidation.run(service, engine=make_engine(ledger, transport, mode="shadow"))
    shadow_again = revalidation.run(service, engine=make_engine(ledger, transport, mode="shadow"))

    live = revalidation.run(service, engine=make_engine(ledger, transport))

    assert shadow_again["asked"] == 0 and shadow_again["skipped_recently_judged"] == 1
    assert live["asked"] == 1 and live["reconfirmed"] == 1, live
    assert service.store.get_claim(claim.id).status == "confirmed"


def test_a_confidence_write_alone_does_not_trigger_a_new_request(env):
    service, ledger = env
    claim = _stale(service, "Stale claim whose confidence the validator keeps rescoring")
    engine = make_engine(ledger, ScriptedTransport(KEEP))
    revalidation.run(service, engine=engine)
    service.store.set_confidence(claim.id, 0.33, details="validator_score=0.330;citations=1")

    again = revalidation.run(service, engine=engine)

    assert again["asked"] == 0 and again["skipped_recently_judged"] == 1, again


def _outcomes(ledger: Path, kind: str) -> list[dict]:
    return DecisionLedger(ledger).query(
        "SELECT decision_id, item_ref, label_source, details_json FROM outcomes WHERE kind = ?", [kind])


def test_frozen_promotions_stop_the_job_before_any_request(env):
    # GOV-B1: a failed quick_check leaves the integrity sentinel and the validator
    # promotes nothing through the broken btree; S1 must not be a way around it.
    service, ledger = env
    claim = _stale(service, "Stale claim while a failed quick_check froze promotions")
    integrity.sentinel_path(service.store.db_path).write_text("quick_check failed (test)", encoding="utf-8")
    transport = ScriptedTransport(CONFIRM)

    summary = revalidation.run(service, engine=make_engine(ledger, transport))

    assert summary["stopped"] == "promotions_frozen" and summary["asked"] == 0, summary
    assert transport.calls == []
    after = service.store.get_claim(claim.id, include_citations=False)
    assert (after.status, after.confidence, after.version) == ("stale", claim.confidence, claim.version)


def test_a_freeze_raised_while_jev_answers_blocks_the_reconfirmation_and_stops(env):
    service, ledger = env
    claims = [_stale(service, f"Stale claim {n} asked just before the integrity check failed") for n in range(3)]
    sentinel = integrity.sentinel_path(service.store.db_path)
    transport = ScriptedTransport(CONFIRM, on_send=lambda _payload: sentinel.write_text("failed", encoding="utf-8"))

    summary = revalidation.run(service, engine=make_engine(ledger, transport), concurrency=1)

    assert summary["stopped"] == "promotions_frozen" and summary["reconfirmed"] == 0, summary
    assert summary["asked"] == 1 and len(transport.calls) == 1
    assert {service.store.get_claim(c.id).status for c in claims} == {"stale"}
    [row] = _outcomes(ledger, "apply_failed")
    assert '"promotions_frozen"' in row["details_json"] and '"reconfirm"' in row["details_json"]


def _declare_outdated(service: MemoryService, claim) -> None:
    """Ingest a correction that declares ``claim`` superseded (files the pending proposal)."""
    service.ingest(f"Correction: {claim.text} is no longer true; the timer was removed",
                   [CitationInput(source="test://s1-correction")], scope="project:mm",
                   supersedes_claim_id=claim.id)


def test_a_claim_declared_outdated_is_neither_asked_nor_reconfirmed(env):
    # GOV-B2: the validator refuses to promote a claim with a pending supersession
    # (promotion_blocked_pending_supersession).  Jev only sees the old text, never
    # the correction, so S1 must not be the path that stamps it confirmed.
    service, ledger = env
    outdated = _stale(service, "The fstrim timer runs weekly on the backup NAS")
    _declare_outdated(service, outdated)
    assert outdated.id in pending_supersession_ids(service.store, use_cache=False)
    transport = ScriptedTransport(CONFIRM)

    summary = revalidation.run(service, engine=make_engine(ledger, transport))

    assert summary["asked"] == 0 and summary["skipped_pending_supersession"] == 1, summary
    assert transport.calls == [], "no spend on a claim code has not cleared for promotion"
    assert service.store.get_claim(outdated.id).status == "stale"


def test_a_supersession_declared_while_jev_answers_blocks_the_reconfirmation(env):
    service, ledger = env
    outdated = _stale(service, "The fstrim timer runs weekly on the archive NAS")
    transport = ScriptedTransport(CONFIRM, on_send=lambda _payload: _declare_outdated(service, outdated))

    summary = revalidation.run(service, engine=make_engine(ledger, transport))

    assert summary["asked"] == 1 and summary["reconfirmed"] == 0, summary
    assert summary["blocked_pending_supersession"] == 1
    assert service.store.get_claim(outdated.id).status == "stale"
    [row] = _outcomes(ledger, "apply_failed")
    assert '"pending_supersession"' in row["details_json"]


def test_a_rejected_supersession_no_longer_blocks_the_claim(env):
    service, ledger = env
    claim = _stale(service, "The fstrim timer runs weekly on the media NAS")
    _declare_outdated(service, claim)
    resolve_steward_proposal(service, action="reject", claim_id=claim.id, actor="operator")

    summary = revalidation.run(service, engine=make_engine(ledger, ScriptedTransport(CONFIRM)))

    assert summary["skipped_pending_supersession"] == 0 and summary["reconfirmed"] == 1, summary
    assert service.store.get_claim(claim.id).status == "confirmed"


@pytest.mark.parametrize("claim_type", ["observation", " Observation "])
def test_observations_are_left_to_the_deterministic_observation_gate(env, claim_type):
    # The validator promotes observations only through review_observation_candidates;
    # S1 must not re-confirm one that decayed to stale.
    service, ledger = env
    observation = _stale(service, "Observation: the recall hook latency rose last week", claim_type=claim_type)
    transport = ScriptedTransport(CONFIRM)

    summary = revalidation.run(service, engine=make_engine(ledger, transport))

    assert summary["asked"] == 0 and transport.calls == [], summary
    assert service.store.get_claim(observation.id).status == "stale"


def test_a_reconfirmation_decay_would_undo_next_cycle_is_not_applied_or_reasked(env):
    # Confidence is set to the validator score; below the decay stale threshold the
    # next decay run re-stales the claim, the version bump makes S1 ask again, and
    # every cycle pays for the same ping-pong.
    service, ledger = env
    claim = _stale(service, "Tmp flag on", confidence=0.1)
    with service.store.connect() as conn:
        conn.execute("DELETE FROM citations WHERE claim_id=?", (claim.id,))
        conn.commit()
    assert validation_score(claim, 0, prior_confidence=0.1) < get_config().stale_threshold
    engine = make_engine(ledger, ScriptedTransport(CONFIRM))

    summary = revalidation.run(service, engine=engine)
    again = revalidation.run(service, engine=engine)

    assert summary["asked"] == 1 and summary["reconfirmed"] == 0, summary
    assert summary["score_below_stale_threshold"] == 1
    after = service.store.get_claim(claim.id, include_citations=False)
    assert (after.status, after.version) == ("stale", claim.version)
    [row] = _outcomes(ledger, "apply_failed")
    assert '"score_below_stale_threshold"' in row["details_json"]
    assert again["asked"] == 0 and again["skipped_recently_judged"] == 1, again


def test_a_store_failure_while_acting_ends_the_run_with_its_summary(env, monkeypatch):
    service, ledger = env
    claims = [_stale(service, f"Stale claim {n} re-confirmed while the disk fills up") for n in range(3)]

    def disk_full(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(service.store, "set_confidence", disk_full)

    summary = revalidation.run(service, engine=make_engine(ledger, ScriptedTransport(CONFIRM)), concurrency=1)

    assert summary["stopped"] == "handle_error" and summary["asked"] == 1, summary
    assert [service.store.get_claim(c.id).status for c in claims[1:]] == ["stale", "stale"]


def test_a_sensitive_stale_claim_is_never_asked_and_stays_on_the_legacy_path(env):
    # Mirrors recall's egress policy: a public claim the sensitivity scan flags
    # (here a stored redaction marker) is never offered to Jev; it stays stale.
    service, ledger = env
    secret = _stale(service, "Billing deploy key note")
    with service.store.connect() as conn:  # disposable fixture: a stored redaction marker
        conn.execute("UPDATE claims SET text=? WHERE id=?",
                     ("Billing deploy key SECRETMARKER [REDACTED:api_key]", secret.id))
        conn.commit()
    plain = _stale(service, "The fstrim timer runs weekly on the office NAS", days_ago=20.0)
    transport = ScriptedTransport(CONFIRM)

    summary = revalidation.run(service, engine=make_engine(ledger, transport))

    assert summary["asked"] == 1 and summary["skipped_sensitive"] == 1, summary
    assert "SECRETMARKER" not in transport.sent_text()
    assert [row[5] for row in _decisions(ledger)] == [f"claim:{plain.id}"]
    assert service.store.get_claim(secret.id).status == "stale"


def _break_event_scan(service: MemoryService, monkeypatch) -> None:
    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(service.store, "list_events", broken)


def test_a_supersession_scan_fault_stops_the_run_before_any_request(env, monkeypatch):
    # Fail closed: with the pending-supersession scan down S1 cannot tell a claim
    # someone declared outdated from a live one, so nothing is asked or promoted.
    service, ledger = env
    claim = _stale(service, "The fstrim timer runs weekly on the lab NAS")
    _break_event_scan(service, monkeypatch)
    transport = ScriptedTransport(CONFIRM)

    summary = revalidation.run(service, engine=make_engine(ledger, transport))

    assert summary["stopped"] == "supersession_scan_failed", summary
    assert summary["asked"] == 0 and summary["reconfirmed"] == 0 and transport.calls == []
    assert service.store.get_claim(claim.id).status == "stale"


def test_a_scan_fault_while_jev_answers_reconfirms_nothing_else_in_the_run(env, monkeypatch):
    service, ledger = env
    claims = [_stale(service, f"The fstrim timer runs weekly on NAS number {n}", days_ago=40.0 - n)
              for n in range(4)]
    broken: list[bool] = []

    def break_once(_payload):
        if not broken:
            broken.append(True)
            _break_event_scan(service, monkeypatch)

    summary = revalidation.run(service, engine=make_engine(ledger, ScriptedTransport(CONFIRM, on_send=break_once)))

    assert summary["stopped"] == "supersession_scan_failed", summary
    assert summary["reconfirmed"] == 0, summary
    assert {service.store.get_claim(c.id).status for c in claims} == {"stale"}
    failed = _outcomes(ledger, "apply_failed")
    assert failed and all('"supersession_scan_failed"' in row["details_json"] for row in failed)
    assert len(failed) == summary["asked"]


def test_the_validator_keeps_ranking_as_before_when_the_scan_fails(env, monkeypatch):
    # The strict mode is S1's alone: the validator and recall still read a fault as "none pending".
    service, _ledger = env
    _break_event_scan(service, monkeypatch)
    from memorymaster.recall import retrieval

    assert pending_supersession_ids(service.store, use_cache=False) == frozenset()
    with pytest.raises(retrieval.PendingSupersessionScanError):
        pending_supersession_ids(service.store, use_cache=False, strict=True)


def test_answers_in_flight_after_a_transient_scan_fault_are_never_acted_on(env, monkeypatch):
    # The fault hits one per-claim re-check only; the scan works again for the
    # answers still in flight, and they must not be re-confirmed in this run.
    service, ledger = env
    claims = [_stale(service, f"The fstrim timer runs weekly on rack NAS {n}", days_ago=40.0 - n)
              for n in range(4)]
    real = service.store.list_events
    armed: list[bool] = []
    sends: list[int] = []

    def flaky(*args, **kwargs):
        if armed:
            armed.clear()
            raise sqlite3.OperationalError("disk I/O error")
        return real(*args, **kwargs)

    monkeypatch.setattr(service.store, "list_events", flaky)

    def arm_on_second_send(_payload):
        sends.append(1)
        if len(sends) == 2:
            armed.append(True)

    summary = revalidation.run(service, engine=make_engine(
        ledger, ScriptedTransport(CONFIRM, on_send=arm_on_second_send)))

    assert summary["stopped"] == "supersession_scan_failed", summary
    assert summary["reconfirmed"] == 1, summary  # the first answer, handled before the fault
    assert [service.store.get_claim(c.id).status for c in claims].count("confirmed") == 1
    failed = _outcomes(ledger, "apply_failed")
    assert len(failed) == summary["asked"] - 1 >= 2, (summary, failed)
    assert all('"supersession_scan_failed"' in row["details_json"] for row in failed)


def test_a_reconfirm_whose_decision_row_is_not_written_is_never_applied(env, monkeypatch):
    """Never act unlogged: Jev answered reconfirm, but the ledger refused the decision row."""
    service, ledger = env
    claim = _stale(service, "The steward cycle runs the validator job before decay in MemoryMaster")
    engine = make_engine(ledger, ScriptedTransport(CONFIRM))
    monkeypatch.setattr(engine.ledger, "write_decision", lambda record, items=(): False)

    summary = revalidation.run(service, engine=engine)

    assert summary["reconfirmed"] == 0 and summary["stopped"] == "ledger_unavailable", summary
    assert summary["fallbacks"] == {"ledger_unavailable": 1}
    assert service.store.get_claim(claim.id, include_citations=False).status == "stale"
    assert not [e for e in service.store.list_events(claim_id=claim.id, limit=50) if e.to_status == "confirmed"]


def test_a_huge_limit_never_builds_one_giant_page_or_ledger_query(env, monkeypatch):
    """Live 2026-09-23: `jev-revalidate --backfill --limit 50000` on 41k stale claims built
    one page and one `IN (...)` of 41k parameters, over SQLite's variable limit, so the
    first ledger read failed and S1 stopped with ledger_unavailable having asked nothing."""
    service, ledger = env
    claims = [_stale(service, f"The fstrim timer runs weekly on archive NAS {n}", days_ago=40.0 - n)
              for n in range(7)]
    monkeypatch.setattr(revalidation, "_PAGE_CAP", 3)
    pages: list[int] = []
    real_page = revalidation._Selection._page

    def spy(self, cursor, size):
        pages.append(size)
        return real_page(self, cursor, size)

    monkeypatch.setattr(revalidation._Selection, "_page", spy)
    engine = make_engine(ledger, ScriptedTransport(KEEP))
    widest: list[int] = []
    real_query = engine.ledger.query

    def query(sql, params=()):
        widest.append(sum(1 for p in params if isinstance(p, str) and p.startswith("claim:")))
        return real_query(sql, params)

    monkeypatch.setattr(engine.ledger, "query", query)

    summary = revalidation.run(service, limit=50_000, backfill=True, engine=engine)

    assert summary["stopped"] is None and summary["asked"] == len(claims), summary
    assert pages and max(pages) <= 3
    assert widest and max(widest) <= 3


def test_the_steward_asks_every_tenant_that_holds_stale_claims(env, tmp_path):
    """Live 2026-09-23: 40,965 of 41,368 stale claims sit in tenant 'personal' while the
    steward hook's service has no tenant, so S1 saw 403. Each tenant with work is listed,
    and a service bound to that tenant is what reaches its claims."""
    service, ledger = env
    untenanted = _stale(service, "The fstrim timer runs weekly on the office NAS")
    personal = _stale(service, "The fstrim timer runs weekly on the home NAS", tenant_id="personal")

    assert revalidation.tenants_with_work(service.store) == [None, "personal"]
    assert revalidation.tenants_with_work(service.store, status="candidate") == []

    tenant_service = MemoryService(tmp_path / "s1.db", workspace_root=tmp_path, tenant_id="personal")
    summary = revalidation.run(tenant_service, engine=make_engine(ledger, ScriptedTransport(KEEP)))
    assert summary["asked"] == 1
    assert [row[5] for row in _decisions(ledger)] == [f"claim:{personal.id}"]
    assert untenanted.id != personal.id


def test_the_steward_template_runs_s1_and_s4_per_tenant():
    template = (Path(__file__).resolve().parents[1] / "memorymaster" / "config_templates" / "hooks"
                / "memorymaster-steward-cycle.py").read_text(encoding="utf-8")
    assert template.count("revalidation.tenants_with_work(svc.store)") == 1
    assert template.count("revalidation.tenants_with_work(svc.store, status=\"candidate\")") == 1
    assert "tenant_id=_tenant" in template
