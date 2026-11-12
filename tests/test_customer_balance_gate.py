"""T-2112: a transactional customer balance is never auto-confirmed and the atlas extractor drops it.

A customer balance is a statement of what an identifiable customer owes / has outstanding / has as
account balance, with the amount (or an explicit zero balance). Every fixture here is invented
(obviously fake names, client 9001). The detector is imported lazily so a red run before the fix
fails on the OUTCOME assertions (status == confirmed), not on a collection error.
All tests use disposable SQLite databases.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memorymaster.bridges import atlas_llm_extractor
from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.promotion_policy import (
    ENV_NON_AUTO_PROMOTABLE_SOURCES,
    PromotionPolicyBlocked,
)
from memorymaster.core.service import MemoryService
from memorymaster.govern import llm_steward
from memorymaster.govern.jobs import curation_drain, validator

# (subject, predicate, object_value, text)
POSITIVES = {
    "extractor_triple": (
        "Juan Perez", "pending_balance", "12,500.00 ARS",
        "Juan Perez has an active internet service with a pending balance of 12,500.00 ARS.",
    ),
    "prose_spanish_client_id": (
        None, None, None,
        "Juan Perez (cliente 9001) mantiene el servicio activo con un saldo de $12.500 pendiente de pago.",
    ),
    "prose_english_client_id": (
        "billing-note", "reminder", None,
        "Client 9001 owes $12,500 for the last two invoices of the fiber plan.",
    ),
    "zero_balance_person": (
        "Juan Perez", "debt_status", None,
        "Juan Perez currently carries a zero debt balance with the provider.",
    ),
}

# Wordings that an over-eager "rule cue" would wrongly excuse (detector-level only).
EXTRA_POSITIVES = {
    "total_before_balance": (
        "Juan Perez", "pending_balance", "12,500.00 ARS",
        "Juan Perez has an active internet service with a total pending balance of 12,500.00 ARS.",
    ),
    "spanish_total_debt": (
        "Juan Perez", None, None, "Juan Perez tiene una deuda total de $12.500 con el proveedor.",
    ),
    "arrow_form": ("Juan Perez", "saldo pendiente", None, "Juan Perez -> saldo pendiente $12.500"),
    "bare_debe_with_amount": (
        "Juan Perez", None, None, "Juan Perez debe $12.500 del servicio de fibra.",
    ),
    "owes_over": ("Juan Perez", None, None, "Juan Perez owes over $12,500 on the fiber plan."),
}

# Real false-positive shapes from the live corpus, described abstractly. All carry a subject
# (a capitalised one where the live shape had it: that is what makes them hard).
# (subject, predicate, object_value, text)
NEGATIVES = {
    "rf_benchmark_period_balance": (
        "RF benchmark", "result", None,
        "The RF benchmark period balance moved from $10,000 to $10,450 over 14 trades, 62 wins and 41 losses.",
    ),
    "technical_debt_loc": (
        "memorymaster", "tech_debt", None,
        "La deuda técnica de memorymaster es de ~4000 LOC repartidas en stores y surfaces.",
    ),
    "api_field_docs": (
        "UISP API", "invoice_fields", None,
        "The invoice endpoint exposes accountOutstanding and saldo_pendiente fields per client record.",
    ),
    "policy_threshold": (
        "Atlas", "reactivation_rule", None,
        "Atlas reactivates a service when the remaining balance is under $1,000 on each overdue invoice.",
    ),
    "aggregate": (
        "Atlas", "suspended_summary", None,
        "Atlas reports 38 suspended clients owe $12.345.678 in total.",
    ),
    "bank_account_conditions": (
        "Santander", "account_conditions", None,
        "Santander requires an average account balance of $1,000,000 to keep the premium package.",
    ),
    "bank_triple_average_balance": (
        "Santander", "average_account_balance", "$1,000,000",
        "Account note for Santander.",
    ),
    "paid_api_prepay": (
        "Anthropic", "billing_note", None,
        "Anthropic requires a prepay balance > $0 before the API accepts calls.",
    ),
    "balanceo_business_name": (
        "Balanceo Sur", "bought_plan", None,
        "Balanceo Sur is a business client that bought the $12,500 plan on 2026-01-02.",
    ),
    "payment_without_balance_term": (
        "Juan Perez", "payment", None,
        "Juan Perez paid $12,500 for the fiber plan on 2026-01-02.",
    ),
}


def _states(subject, predicate, object_value, text) -> bool:
    from memorymaster.core.promotion_policy import states_customer_balance

    return states_customer_balance(subject, predicate, object_value, text)


@pytest.fixture()
def svc(tmp_path: Path, monkeypatch) -> MemoryService:
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.delenv(ENV_NON_AUTO_PROMOTABLE_SOURCES, raising=False)
    monkeypatch.delenv("MEMORYMASTER_INLINE_VALIDATION", raising=False)
    monkeypatch.setattr(validator, "load_classifier", lambda: None)
    service = MemoryService(str(tmp_path / "balance.db"), workspace_root=str(tmp_path))
    service.init_db()
    return service


def _claim(svc, text, *, subject=None, predicate=None, object_value=None,
           source_agent="claude-session", confidence=0.8):
    return svc.ingest(
        text=text,
        citations=[CitationInput(source="session://t", locator="turn", excerpt=text[:30])],
        subject=subject, predicate=predicate, object_value=object_value,
        scope="project:x", confidence=confidence, source_agent=source_agent,
    ).id


def _status_of(svc, claim_id) -> str:
    return svc.store.get_claim(claim_id, include_citations=False).status


def _harmless(svc):
    return _claim(svc, "Release deadline is 2026-04-01 according to a session",
                  subject="rel-b", predicate="deadline", object_value="2026-04-01")


def _positive_claim(svc, name):
    subject, predicate, object_value, text = POSITIVES[name]
    return _claim(svc, text, subject=subject, predicate=predicate, object_value=object_value)


# --- detector ----------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(POSITIVES))
def test_detector_flags_customer_balances(name):
    assert _states(*POSITIVES[name]) is True


@pytest.mark.parametrize("name", sorted(EXTRA_POSITIVES))
def test_detector_flags_wordings_a_loose_rule_cue_would_excuse(name):
    assert _states(*EXTRA_POSITIVES[name]) is True


def test_detector_does_not_flag_modal_debe():
    assert _states("Juan Perez", None, None, "Juan Perez debe 3 veces revisar los $1.000 del plan.") is False


@pytest.mark.parametrize("name", sorted(NEGATIVES))
def test_detector_does_not_flag_false_positive_shapes(name):
    assert _states(*NEGATIVES[name]) is False


def test_detector_needs_the_conjunction_not_one_part():
    assert _states("Juan Perez", None, None, "Juan Perez asked about the balance of the plan.") is False  # no amount
    assert _states("Juan Perez", None, None, "Juan Perez moved to the $12,500 plan.") is False  # no balance term
    assert _states(None, None, None, "The pending balance is $12,500.") is False  # no identified party
    assert _states(None, None, None, "Cliente 9001 compró el plan.") is False  # no balance, no amount


def test_detector_party_must_be_in_the_same_window():
    far = "x" * 400
    text = f"Client 9001 is a fiber customer. {far} The reconciliation shows a balance of $12,500."
    assert _states(None, None, None, text) is False


def test_detector_tolerates_missing_fields():
    assert _states(None, None, None, None) is False
    assert _states("", "", "", "") is False


# --- lifecycle gateway ---------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(POSITIVES))
def test_transition_blocks_automatic_confirmation_but_not_an_explicit_actor(svc, name):
    cid = _positive_claim(svc, name)
    with pytest.raises(PromotionPolicyBlocked):
        transition_claim(svc.store, cid, "confirmed", reason="auto", event_type="validator")
    assert _status_of(svc, cid) == "candidate"
    assert transition_claim(svc.store, cid, "confirmed", reason="explicit", event_type="transition",
                            event_payload={"actor": "operator"}).status == "confirmed"


@pytest.mark.parametrize("name", sorted(NEGATIVES))
def test_negative_shapes_still_promote_through_the_lifecycle(svc, name):
    subject, predicate, object_value, text = NEGATIVES[name]
    cid = _claim(svc, text, subject=subject, predicate=predicate, object_value=object_value)
    assert transition_claim(svc.store, cid, "confirmed", reason="auto", event_type="validator").status == "confirmed"


# --- validator cycle -------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(POSITIVES))
def test_validator_holds_a_customer_balance_and_still_promotes_others(svc, name):
    held = _positive_claim(svc, name)
    other = _harmless(svc)

    first = validator.run(svc.store, min_citations=1, min_score=0.5)
    second = validator.run(svc.store, min_citations=1, min_score=0.5)

    assert _status_of(svc, other) == "confirmed", "control claim must still be promoted"
    assert _status_of(svc, held) == "candidate"
    assert first["held_by_customer_balance"] == 1 and second["held_by_customer_balance"] == 1
    holds = [e for e in svc.store.list_events(claim_id=held, event_type="validator", limit=20)
             if e.details == "promotion_blocked_customer_balance"]
    assert len(holds) == 1, "hold is audited once, not once per cycle"


def test_validator_promotes_every_negative_shape(svc):
    ids = {}
    for name, (subject, predicate, object_value, text) in NEGATIVES.items():
        ids[name] = _claim(svc, text, subject=subject, predicate=predicate, object_value=object_value)
    result = validator.run(svc.store, min_citations=1, min_score=0.5)
    assert result.get("held_by_customer_balance", 0) == 0
    assert {name: _status_of(svc, cid) for name, cid in ids.items()} == {name: "confirmed" for name in ids}


# --- MCP fast path -----------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(POSITIVES))
def test_validate_now_leaves_a_customer_balance_candidate_with_a_distinct_reason(svc, name):
    held = _positive_claim(svc, name)
    report = svc.validate_now(held)
    assert report["status"] == "candidate"
    assert report.get("held") == "customer_balance"
    assert "error" not in report
    assert svc.validate_now(_harmless(svc))["status"] == "confirmed"


# --- llm steward (raw SQL confirm) ------------------------------------------------------------

def _cas_confirm(svc, claim_id, **fields):
    version = svc.store.get_claim(claim_id, include_citations=False).version
    conn = sqlite3.connect(svc.store.db_path)
    try:
        ok = llm_steward._confirm_candidate_cas(
            conn, claim_id=claim_id, version=version, confidence=0.9, **fields,
        )
        conn.commit()
    finally:
        conn.close()
    return ok


def test_llm_steward_cas_refuses_a_customer_balance_and_still_confirms_ordinary_text(svc):
    held = _positive_claim(svc, "extractor_triple")
    assert _cas_confirm(svc, held, subject="Juan Perez", predicate="pending_balance",
                        object_value="12,500.00 ARS") is False
    assert _status_of(svc, held) == "candidate"
    ordinary = _claim(svc, "Ordinary candidate")
    assert _cas_confirm(svc, ordinary, subject="s", predicate="p", object_value=None) is True


def test_llm_steward_cas_judges_the_stored_triple_the_llm_wrote(svc):
    """A bare candidate whose rewrite turns it into a balance triple is refused too."""
    cid = _claim(svc, "Juan Perez has a pending balance of 12,500.00 ARS with the provider.")
    assert _cas_confirm(svc, cid, subject="Juan Perez", predicate="pending_balance",
                        object_value="12,500.00 ARS") is False


def _steward_run(svc, source_text, extractions):
    original = llm_steward.extract_claim

    def fake_extract(provider, api_key, model, claim_id, text, base_url="", key_rotator=None,
                     use_llm_provider=False):
        return llm_steward.ExtractionResult(
            claim_id=claim_id, original_text=text, extractions=extractions, action="confirm", raw_response="")

    llm_steward.extract_claim = fake_extract
    try:
        cid = _claim(svc, source_text)
        stats = llm_steward.run_steward(svc.store.db_path, api_key="x", provider="gemini", limit=10,
                                        delay=0.0, auto_validate=False)
    finally:
        llm_steward.extract_claim = original
    return cid, stats


def _subjects(svc):
    return {c.subject for c in svc.list_claims(limit=50, include_archived=True)}


def test_llm_steward_run_holds_a_first_extraction_that_states_a_balance(svc):
    cid, stats = _steward_run(
        svc, "Juan Perez has an active internet service with a pending balance of 12,500.00 ARS.",
        [{"subject": "Juan Perez", "predicate": "pending_balance", "object_value": "12,500.00 ARS",
          "confidence": 0.8}],
    )
    assert _status_of(svc, cid) == "candidate"
    assert stats["held_by_customer_balance"] == 1 and stats["confirmed"] == 0


def test_llm_steward_run_does_not_insert_a_balance_extra_extraction_as_confirmed(svc):
    cid, stats = _steward_run(
        svc, "Juan Perez has a pending balance of 12,500.00 ARS and the release deadline is 2026-04-01.",
        [{"subject": "rel-b", "predicate": "deadline", "object_value": "2026-04-01", "confidence": 0.8},
         {"subject": "Juan Perez", "predicate": "pending_balance", "object_value": "12,500.00 ARS",
          "confidence": 0.8}],
    )
    assert _status_of(svc, cid) == "confirmed", "the harmless first extraction still confirms"
    assert "Juan Perez" not in _subjects(svc)
    assert stats["held_by_customer_balance"] == 1


# --- curation drain -----------------------------------------------------------------------------

def test_a_customer_balance_conflict_winner_is_not_promoted_and_does_not_supersede_the_rival(svc):
    rival = _claim(svc, "the older confirmed fact with less confidence", subject="Juan Perez",
                   predicate="pending_balance", object_value="12,500.00 ARS", confidence=0.3)
    transition_claim(svc.store, rival, "confirmed", reason="fixture", event_payload={"actor": "operator"})
    winner = _claim(svc, "Juan Perez has an active internet service with a pending balance of 12,500.00 ARS.",
                    subject="Juan Perez", predicate="pending_balance", object_value="12,500.00 ARS",
                    confidence=0.95)
    transition_claim(svc.store, winner, "conflicted", reason="fixture", event_type="validator")
    svc.store.record_event(claim_id=winner, event_type="validator",
                           details=f"conflicts_with_confirmed_claim:{rival}")

    result = curation_drain.drain_conflicts(svc, apply=True)

    assert _status_of(svc, winner) == "conflicted"
    assert _status_of(svc, rival) == "confirmed"
    assert result["held_by_customer_balance"] == 1 and result["resolved_won"] == 0


# --- atlas extractor (source) ----------------------------------------------------------------------

def _seed_evidence(service: MemoryService, *, text: str):
    source = service.upsert_external_source(source_type="whatsapp", display_name="primary")
    item = service.upsert_source_item(
        source_id=source.id, source_item_id="msg-1", item_type="whatsapp", sender_name="Pablo",
        occurred_at="2026-06-20T10:00:00", text=text,
    )
    return service.add_evidence_item(
        source_item_id=item.id, evidence_type="message_text", text=text, provider="whatsapp_live",
    )


BALANCE_ROW = {
    "type": "fact", "subject": "Juan Perez", "predicate": "pending_balance", "object": "12,500.00 ARS",
    "text": "Juan Perez has an active internet service with a pending balance of 12,500.00 ARS.",
    "confidence": 0.8,
}
HARMLESS_ROW = {
    "type": "commitment", "subject": "Pablo", "predicate": "will_send", "object": "payment receipt",
    "text": "A contact named Pablo committed to sending the payment receipt on Friday the 26th.",
    "confidence": 0.8, "event_time": "2026-06-26",
}


def _run_extractor(tmp_path, monkeypatch, rows):
    service = MemoryService(tmp_path / "atlas.db", workspace_root=tmp_path)
    service.init_db()
    evidence = _seed_evidence(service, text="synthetic message body")
    monkeypatch.setattr(atlas_llm_extractor, "call_llm", lambda prompt, text: json.dumps(rows))
    result = atlas_llm_extractor.extract_atlas_claims_llm(
        service, scope="user", evidence_items=[evidence], evidence_ids={evidence.id},
    )
    return service, result


def test_atlas_extractor_drops_a_customer_balance_but_ingests_its_harmless_sibling(tmp_path, monkeypatch):
    service, result = _run_extractor(tmp_path, monkeypatch, [BALANCE_ROW, HARMLESS_ROW])
    assert result.ingested == 1 and result.emitted == 1
    assert result.invalid_rows == 1 and result.partial == 1
    assert [c.subject for c in result.claims] == ["Pablo"]
    texts = [c.text for c in service.list_claims(limit=50, include_archived=True)]
    assert not any("pending balance" in t for t in texts)


def test_atlas_extractor_item_with_only_a_balance_is_not_degraded(tmp_path, monkeypatch):
    """``degraded`` makes the capture worker retry the job (re-sending the mail to the provider)."""
    service, result = _run_extractor(tmp_path, monkeypatch, [BALANCE_ROW])
    assert result.ingested == 0 and result.degraded == 0 and result.invalid_rows == 1
    assert service.list_claims(limit=50, include_archived=True) == []


def test_atlas_prompt_tells_the_model_not_to_emit_customer_balances():
    prompt = atlas_llm_extractor._PROMPT_BODY.lower()
    assert "balance" in prompt and "customer" in prompt
