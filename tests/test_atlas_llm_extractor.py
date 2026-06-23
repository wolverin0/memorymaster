"""Hermetic tests for the LLM-driven Atlas typed-entity extractor.

Spec: ``.planning/ATLAS-LLM-EXTRACTOR-SPEC.md`` §5. These tests prove the
extractor replaces the deterministic keyword matcher's failure modes:

  * It emits TYPED entities with REAL subjects (never ``subject="whatsapp_contact"``
    or a bare source name) — case (1).
  * Bot/newsletter fixtures produce ZERO claims — the misclassification bug that
    flooded the live VM with 5,544 junk wrappers cannot recur — case (2).
  * Empty / malformed / raising LLM output degrades gracefully — no crash, no
    fallback junk claim — case (3).
  * Every ingest still routes through ``service.ingest`` so the sensitivity
    filter catches a secret-shaped string — case (4).
  * The idempotency key is stable across runs — case (5).
  * Citations are provider-aware (the hardcoded ``whatsapp://`` bug is fixed) —
    case (6).
  * ``dry_run`` drafts without ingesting — case (7).
  * The CLI dispatches ``--extractor deterministic`` to the old path and defaults
    to the LLM path — case (8).

ALL tests are hermetic: ``call_llm`` is monkeypatched to canned JSON (no network /
no real provider) and the DB is a throwaway ``MemoryService`` on ``tmp_path``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest

from memorymaster.bridges import atlas_llm_extractor
from memorymaster.bridges.atlas_llm_extractor import (
    AtlasLlmExtractionResult,
    extract_atlas_claims_llm,
)
from memorymaster.core.service import MemoryService

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                          #
# --------------------------------------------------------------------------- #


def _service(tmp_path: Path) -> MemoryService:
    service = MemoryService(tmp_path / "t.db", workspace_root=tmp_path)
    service.init_db()
    return service


def _seed_evidence(
    service: MemoryService,
    *,
    source_type: str,
    provider: str | None,
    text: str,
    sender_name: str | None = "Pablo",
    occurred_at: str | None = "2026-06-20T10:00:00",
    source_item_id: str = "msg-1",
):
    """Seed one external source + source item + evidence row; return the evidence."""
    source = service.upsert_external_source(source_type=source_type, display_name="primary")
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id=source_item_id,
        item_type=source_type,
        sender_name=sender_name,
        occurred_at=occurred_at,
        text=text,
    )
    return service.add_evidence_item(
        source_item_id=item.id,
        evidence_type="message_text",
        text=text,
        provider=provider,
    )


def _canned(rows_by_call: list[object] | object) -> Callable[[str, str], str]:
    """Build a ``call_llm`` stand-in returning canned raw strings.

    ``rows_by_call`` is either a single value (returned for every call) or a list
    consumed one entry per call. Each entry may be a python object (serialised to
    JSON) or a raw string (returned verbatim — for malformed-output tests).
    """

    state = {"i": 0}
    is_sequence = isinstance(rows_by_call, list)

    def _fake_call_llm(prompt: str, text: str) -> str:
        if is_sequence:
            value = rows_by_call[state["i"]]
            state["i"] += 1
        else:
            value = rows_by_call
        if isinstance(value, str):
            return value
        return json.dumps(value)

    return _fake_call_llm


def _patch_llm(monkeypatch, fake: Callable[[str, str], str]) -> None:
    monkeypatch.setattr(atlas_llm_extractor, "call_llm", fake)


# --------------------------------------------------------------------------- #
# (1) Typed extraction — real subject + event_time                            #
# --------------------------------------------------------------------------- #


def test_whatsapp_body_yields_commitment_with_real_subject_and_event_time(
    tmp_path: Path, monkeypatch
) -> None:
    """A WhatsApp commitment body -> a 'commitment' claim with a REAL subject."""
    service = _service(tmp_path)
    _seed_evidence(
        service,
        source_type="whatsapp",
        provider="whatsapp",
        text="Hola, te confirmo que te paso el comprobante del pago el viernes 26.",
        sender_name="Pablo",
    )
    _patch_llm(
        monkeypatch,
        _canned(
            [
                {
                    "type": "commitment",
                    "subject": "Pablo",
                    "predicate": "will_send",
                    "object": "payment receipt",
                    "text": "Pablo committed to sending the payment receipt on Friday the 26th.",
                    "confidence": 0.8,
                    "event_time": "2026-06-26",
                }
            ]
        ),
    )

    result = extract_atlas_claims_llm(service, scope="project:atlas-test")

    assert result.scanned == 1
    assert result.matched == 1
    assert result.ingested == 1
    assert result.emitted == 1
    assert result.degraded == 0
    claim = result.claims[0]
    assert claim.claim_type == "commitment"
    assert claim.subject == "Pablo"  # real entity, NOT "whatsapp_contact"
    assert claim.predicate == "will_send"
    assert claim.event_time == "2026-06-26"
    assert claim.scope == "project:atlas-test"


def test_email_decision_body_yields_decision_claim(tmp_path: Path, monkeypatch) -> None:
    """An email announcing a decision -> a 'decision' claim with a real subject."""
    service = _service(tmp_path)
    _seed_evidence(
        service,
        source_type="gmail",
        provider="gmail",
        text="We have decided to migrate Atlas to Postgres next quarter.",
        sender_name="María González",
    )
    _patch_llm(
        monkeypatch,
        _canned(
            [
                {
                    "type": "decision",
                    "subject": "Atlas",
                    "predicate": "will_migrate_to",
                    "object": "Postgres",
                    "text": "The team decided to migrate Atlas to Postgres next quarter.",
                    "confidence": 0.75,
                }
            ]
        ),
    )

    result = extract_atlas_claims_llm(service, scope="project:atlas-test")

    assert result.ingested == 1
    claim = result.claims[0]
    assert claim.claim_type == "decision"
    assert claim.subject == "Atlas"
    assert claim.predicate == "will_migrate_to"


# --------------------------------------------------------------------------- #
# (2) Noise rejection — the core regression                                   #
# --------------------------------------------------------------------------- #


def test_bot_notification_yields_zero_claims(tmp_path: Path, monkeypatch) -> None:
    """vercel[bot] / newsletter fixture -> LLM returns [] -> 0 claims ingested.

    WHY: this is the exact misclassification that produced 5,544 junk wrappers on
    the live VM (claim mm-d993). An empty array MUST ingest nothing — proving the
    bug cannot recur.
    """
    service = _service(tmp_path)
    _seed_evidence(
        service,
        source_type="gmail",
        provider="gmail",
        text="from vercel[bot]: Re:[repo] Deployment succeeded for commit abc123.",
        sender_name="vercel[bot]",
    )
    _patch_llm(monkeypatch, _canned("[]"))

    result = extract_atlas_claims_llm(service, scope="project:atlas-test")

    assert result.scanned == 1
    assert result.matched == 0
    assert result.ingested == 0
    assert result.emitted == 0
    assert result.degraded == 0  # an empty array is a VALID answer, not a degrade
    assert result.claims == []


# --------------------------------------------------------------------------- #
# (3) Graceful degrade                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad_output",
    [
        "",  # empty string
        "   ",  # whitespace only
        "not json at all { ] [ }",  # malformed
        '{"type": "person"}',  # a bare object, no array — but still parseable to []? see below
    ],
)
def test_malformed_or_empty_llm_output_degrades(
    tmp_path: Path, monkeypatch, bad_output: str
) -> None:
    """Empty / malformed LLM output -> item skipped, no crash, no junk claim.

    For the truly unparseable / empty shapes ``degraded`` increments and nothing
    is ingested. (A lone object that parse_json_response coerces to a list still
    ingests nothing here because it carries no valid typed fields.)
    """
    service = _service(tmp_path)
    _seed_evidence(
        service,
        source_type="whatsapp",
        provider="whatsapp",
        text="some real body text that the llm chokes on",
    )
    _patch_llm(monkeypatch, _canned(bad_output))

    result = extract_atlas_claims_llm(service, scope="project:atlas-test")

    assert result.scanned == 1
    assert result.ingested == 0
    assert result.claims == []


def test_call_llm_raises_is_caught_and_degraded(tmp_path: Path, monkeypatch) -> None:
    """call_llm raising -> caught, item skipped, degraded incremented, no crash."""
    service = _service(tmp_path)
    _seed_evidence(
        service,
        source_type="whatsapp",
        provider="whatsapp",
        text="a body that triggers a provider exception",
    )

    def _boom(prompt: str, text: str) -> str:
        raise RuntimeError("provider exploded")

    _patch_llm(monkeypatch, _boom)

    result = extract_atlas_claims_llm(service, scope="project:atlas-test")

    assert result.scanned == 1
    assert result.degraded == 1
    assert result.ingested == 0
    assert result.claims == []


# --------------------------------------------------------------------------- #
# (4) Sensitivity routing — proves we go through service.ingest               #
# --------------------------------------------------------------------------- #


def test_secret_in_claim_text_is_caught_by_ingest_filter(
    tmp_path: Path, monkeypatch
) -> None:
    """A secret-shaped claim text is redacted by the ingest sensitivity filter.

    WHY: this only passes if the extractor routes the claim through
    ``service.ingest`` (the last line of defense). The OpenAI-key-shaped string
    must NOT be stored verbatim.
    """
    secret = "sk-" + "A" * 32
    service = _service(tmp_path)
    _seed_evidence(
        service,
        source_type="gmail",
        provider="gmail",
        text="benign body; the secret only appears in the LLM-extracted claim",
    )
    _patch_llm(
        monkeypatch,
        _canned(
            [
                {
                    "type": "fact",
                    "subject": "Acme API",
                    "predicate": "uses_key",
                    "object": "production token",
                    "text": f"The Acme API production key is {secret}.",
                    "confidence": 0.9,
                }
            ]
        ),
    )

    result = extract_atlas_claims_llm(service, scope="project:atlas-test")

    assert result.ingested == 1
    stored = result.claims[0].text
    assert secret not in stored  # the raw key never lands verbatim
    assert "[REDACTED:" in stored  # proves the ingest filter fired


# --------------------------------------------------------------------------- #
# (5) Idempotency                                                              #
# --------------------------------------------------------------------------- #


def test_running_twice_ingests_each_claim_once(tmp_path: Path, monkeypatch) -> None:
    """Stable idempotency key -> two runs return the same claim, ingested once."""
    service = _service(tmp_path)
    _seed_evidence(
        service,
        source_type="whatsapp",
        provider="whatsapp",
        text="Pablo confirmed the contract is signed.",
    )
    _patch_llm(
        monkeypatch,
        _canned(
            {
                "type": "fact",
                "subject": "Pablo",
                "predicate": "signed",
                "object": "the contract",
                "text": "Pablo signed the contract.",
                "confidence": 0.8,
            }
        ),
    )

    first = extract_atlas_claims_llm(service, scope="project:atlas-test")
    second = extract_atlas_claims_llm(service, scope="project:atlas-test")

    assert first.ingested == 1
    assert second.ingested == 1
    assert first.claims[0].id == second.claims[0].id  # same row, not a duplicate


# --------------------------------------------------------------------------- #
# (6) Provider-aware citation                                                  #
# --------------------------------------------------------------------------- #


def test_gmail_provider_yields_gmail_scheme_not_whatsapp(
    tmp_path: Path, monkeypatch
) -> None:
    """A gmail-provider evidence -> gmail:// citation (the hardcoded whatsapp bug)."""
    service = _service(tmp_path)
    _seed_evidence(
        service,
        source_type="gmail",
        provider="gmail",
        text="María confirmed the meeting.",
        sender_name="María",
        source_item_id="email-7",
    )
    _patch_llm(
        monkeypatch,
        _canned(
            [
                {
                    "type": "event",
                    "subject": "María",
                    "predicate": "confirmed",
                    "object": "the meeting",
                    "text": "María confirmed the meeting.",
                    "confidence": 0.7,
                }
            ]
        ),
    )

    result = extract_atlas_claims_llm(service, scope="project:atlas-test")

    source = result.claims[0].citations[0].source
    assert source.startswith("gmail://")
    assert "whatsapp://" not in source


def test_unknown_provider_falls_back_to_atlas_scheme(
    tmp_path: Path, monkeypatch
) -> None:
    """A provider with no scheme mapping -> default atlas:// citation."""
    service = _service(tmp_path)
    _seed_evidence(
        service,
        source_type="slack",
        provider="slack",
        text="Pablo asked about the budget.",
    )
    _patch_llm(
        monkeypatch,
        _canned(
            [
                {
                    "type": "topic",
                    "subject": "budget",
                    "predicate": "discussed_by",
                    "object": "Pablo",
                    "text": "Pablo asked about the budget.",
                    "confidence": 0.6,
                }
            ]
        ),
    )

    result = extract_atlas_claims_llm(service, scope="project:atlas-test")
    assert result.claims[0].citations[0].source.startswith("atlas://")


# --------------------------------------------------------------------------- #
# (7) dry_run                                                                  #
# --------------------------------------------------------------------------- #


def test_dry_run_drafts_without_ingesting(tmp_path: Path, monkeypatch) -> None:
    """dry_run=True -> emitted counted, NOTHING ingested, DB stays empty."""
    service = _service(tmp_path)
    _seed_evidence(
        service,
        source_type="whatsapp",
        provider="whatsapp",
        text="Pablo will send the invoice tomorrow.",
    )
    _patch_llm(
        monkeypatch,
        _canned(
            [
                {
                    "type": "commitment",
                    "subject": "Pablo",
                    "predicate": "will_send",
                    "object": "invoice",
                    "text": "Pablo will send the invoice tomorrow.",
                    "confidence": 0.8,
                    "event_time": "2026-06-21",
                }
            ]
        ),
    )

    result = extract_atlas_claims_llm(service, scope="project:atlas-test", dry_run=True)

    assert result.emitted == 1
    assert result.ingested == 0
    assert result.claims == []
    # Nothing actually persisted.
    assert service.list_claims(limit=50) == [] or len(service.list_claims(limit=50)) == 0


# --------------------------------------------------------------------------- #
# (8) CLI dispatch                                                             #
# --------------------------------------------------------------------------- #


def test_cli_default_uses_llm_path(tmp_path: Path, monkeypatch) -> None:
    """Default (no --extractor) dispatches to extract_atlas_claims_llm."""
    from memorymaster.surfaces.cli import main

    db = tmp_path / "cli.db"
    called = {"llm": 0, "deterministic": 0}

    def _fake_llm(service, *, scope, limit=200, model=None, dry_run=False):
        called["llm"] += 1
        return AtlasLlmExtractionResult(
            scanned=0, matched=0, ingested=0, degraded=0, emitted=0, claims=[]
        )

    def _fake_det(service, *, scope=None, limit=200):  # pragma: no cover - must NOT run
        called["deterministic"] += 1
        raise AssertionError("deterministic path called on default dispatch")

    monkeypatch.setattr(atlas_llm_extractor, "extract_atlas_claims_llm", _fake_llm)
    monkeypatch.setattr(
        "memorymaster.bridges.atlas_claim_extractor.extract_atlas_claims_from_evidence",
        _fake_det,
    )

    assert main(["--db", str(db), "init-db"]) == 0
    assert main(["--db", str(db), "extract-atlas-claims", "--scope", "project:cli"]) == 0
    assert called["llm"] == 1
    assert called["deterministic"] == 0


def test_cli_extractor_deterministic_uses_old_path(tmp_path: Path, monkeypatch) -> None:
    """--extractor deterministic dispatches to the preserved keyword matcher."""
    from memorymaster.surfaces.cli import main

    db = tmp_path / "cli.db"
    called = {"llm": 0, "deterministic": 0}

    def _fake_det(service, *, scope=None, limit=200):
        called["deterministic"] += 1
        from memorymaster.bridges.atlas_claim_extractor import AtlasClaimExtractionResult

        return AtlasClaimExtractionResult(scanned=0, matched=0, ingested=0, claims=[])

    def _fake_llm(service, *, scope, limit=200, model=None, dry_run=False):  # pragma: no cover
        called["llm"] += 1
        raise AssertionError("llm path called on --extractor deterministic")

    monkeypatch.setattr(
        "memorymaster.bridges.atlas_claim_extractor.extract_atlas_claims_from_evidence",
        _fake_det,
    )
    monkeypatch.setattr(atlas_llm_extractor, "extract_atlas_claims_llm", _fake_llm)

    assert main(["--db", str(db), "init-db"]) == 0
    assert (
        main(
            [
                "--db",
                str(db),
                "extract-atlas-claims",
                "--scope",
                "project:cli",
                "--extractor",
                "deterministic",
            ]
        )
        == 0
    )
    assert called["deterministic"] == 1
    assert called["llm"] == 0


# --- case (2b): the validation guard itself rejects junk/source-name subjects ---
# Regression for mm-d993: the original bug ingested claims with
# subject="whatsapp_contact". Even if the LLM emits that shape, _validate_row
# MUST drop it before ingest. (The review flagged that no test exercised this.)
@pytest.mark.parametrize(
    "subject",
    [
        "whatsapp_contact",   # the exact original-bug subject
        "WhatsApp",
        "Gmail",
        "Google Drive",
        "email_contact",
        "some_random_contact",  # any *_contact placeholder
        "contact",
        "unknown",
        "",
    ],
)
def test_validate_row_rejects_junk_and_source_name_subjects(subject):
    row = {
        "type": "commitment",
        "subject": subject,
        "predicate": "reported",
        "object": "x",
        "text": "Atlas commitment evidence from vercel[bot]: Re:[repo]",
    }
    assert atlas_llm_extractor._validate_row(row) is None


def test_validate_row_accepts_real_entity_subject():
    row = {
        "type": "person",
        "subject": "Pablo Lujan",
        "predicate": "is",
        "object": "technical contact at io.net.ar",
        "text": "Pablo Lujan is a technical contact at io.net.ar.",
    }
    typed = atlas_llm_extractor._validate_row(row)
    assert typed is not None
    assert typed.subject == "Pablo Lujan"
    assert typed.claim_type == "person"
