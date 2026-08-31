"""Governed personal-skill-v1 lifecycle and projection tests.

These tests anchor the P3 contract: recurring evidence may create one skill
candidate, generic validation cannot promote it, explicit audited approval can,
and updates supersede immutable prior versions. Projection writes only beneath
an explicit MemoryMaster staging root.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.core.security import SensitiveMetadataError
from memorymaster.capture import CaptureRepository
from memorymaster.stores._storage_shared import ConcurrentModificationError
from memorymaster.govern.jobs import validator
from memorymaster.knowledge.rule_miner import rule_fingerprint
from memorymaster.knowledge.rule_observations import record_rule_observation
from memorymaster.knowledge.rules import build_rule_fields
from memorymaster.knowledge.skills import (
    SkillValidationError,
    approve_skill_candidate,
    build_skill_fields,
    collect_skill_proposal_inputs,
    export_confirmed_skills,
    parse_skill,
    propose_skill,
    recall_skills,
    reject_skill_candidate,
    review_due_skills,
    review_skill_proposal,
)
from memorymaster.surfaces.cli import main as cli_main


@pytest.fixture
def service(tmp_path: Path) -> MemoryService:
    result = MemoryService(tmp_path / "skills.db", workspace_root=tmp_path)
    result.init_db()
    return result


def _payload(**overrides):
    payload = {
        "schema": "personal-skill-v1",
        "slug": "safe-release-check",
        "title": "Safe release check",
        "when_to_use": "Before preparing a MemoryMaster release.",
        "when_not_to_use": "For changes that are not being released.",
        "inputs": ["candidate commit"],
        "prerequisites": ["clean disposable database"],
        "workflow": ["Run focused tests.", "Run the full release gate."],
        "decision_rules": ["Stop when an invariant fails."],
        "expected_output": "A reproducible release evidence report.",
        "validation": ["Confirm tests, Ruff, and diff checks pass."],
        "pitfalls": ["Do not treat skipped infrastructure as green."],
        "recovery": ["Keep the candidate unpublished and fix the gate."],
        "quality_scores": {
            "recurrence": 16,
            "reusability": 16,
            "executability": 16,
            "validation": 16,
            "safety": 16,
        },
    }
    payload.update(overrides)
    return payload


def _reviewer_json(**payload_overrides) -> str:
    return json.dumps({"classification": "skill", "payload": _payload(**payload_overrides)})


def _rule(
    service: MemoryService, *, correction_count: int = 2, root_sessions: int = 3
):
    trigger = "preparing a MemoryMaster release"
    action = "run the reproducible release gate"
    claim = service.ingest(
        **build_rule_fields(trigger, action, "partial checks do not prove a release"),
        citations=[CitationInput(source="verbatim", locator="correction-a")],
        scope="project:memorymaster",
        confidence=0.7,
        source_agent="rule-miner",
    )
    fingerprint = rule_fingerprint(trigger, action)
    with sqlite3.connect(service.store.db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS rule_stats "
            "(rule_fingerprint TEXT PRIMARY KEY, correction_count INTEGER NOT NULL DEFAULT 1, "
            "last_mined TEXT NOT NULL, confidence_at_last_mine REAL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO rule_stats "
            "(rule_fingerprint, correction_count, last_mined) VALUES (?, ?, ?)",
            (fingerprint, correction_count, "2026-08-07T00:00:00+00:00"),
        )
        for index in range(root_sessions):
            record_rule_observation(
                conn,
                rule_fingerprint=fingerprint,
                provider="claude",
                root_session_id=f"root-session-{index}",
                project_scope="project:memorymaster",
                source_ref=f"verbatim:{index}",
                evidence_hash=f"{index + 1:064x}",
            )
    return claim


def test_personal_skill_schema_hash_and_round_trip(service: MemoryService) -> None:
    fields = build_skill_fields(_payload(), supporting_claim_ids=[7, 3])
    stored = json.loads(fields["object_value"])

    assert fields["claim_type"] == "skill"
    assert fields["predicate"] == "applies_when"
    assert stored["supporting_claim_ids"] == [3, 7]
    assert len(stored["content_sha256"]) == 64
    assert stored["skill_version"] == 1

    claim = service.ingest(
        **fields,
        citations=[CitationInput(source="claim", locator="claim:3")],
        scope="project:memorymaster",
        source_agent="skill-reviewer",
    )
    parsed = parse_skill(service.store.get_claim(claim.id))
    assert parsed is not None
    assert parsed["slug"] == "safe-release-check"
    assert parsed["claim_id"] == claim.id


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"schema": "invented-v9"}, "schema"),
        ({"slug": "../escape"}, "slug"),
        ({"validation": []}, "validation"),
        ({"quality_scores": {"recurrence": 20}}, "quality_scores"),
    ],
)
def test_personal_skill_validator_fails_closed(change, message) -> None:
    with pytest.raises(SkillValidationError, match=message):
        build_skill_fields(_payload(**change), supporting_claim_ids=[1, 2])


def test_review_unknown_or_non_skill_classification_is_diagnostic(service: MemoryService) -> None:
    unknown = review_skill_proposal(
        service,
        classification="made_up",
        payload=_payload(),
        supporting_claim_ids=[1, 2],
        scope="project:memorymaster",
    )
    memory = review_skill_proposal(
        service,
        classification="memory",
        payload=_payload(),
        supporting_claim_ids=[1, 2],
        scope="project:memorymaster",
    )

    assert unknown == {"ok": False, "created": False, "reason": "unknown_classification"}
    assert memory == {"ok": True, "created": False, "reason": "classified_as_memory"}
    assert service.store.list_claims(status="candidate", limit=20) == []
    details = {event.details for event in service.store.list_events(limit=20)}
    assert "skill_reviewer_unknown_output" in details
    assert "skill_reviewer_not_skill" in details


def test_bounded_llm_reviewer_creates_once_and_skips_used_evidence(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    rule = _rule(service)
    calls: list[tuple[str, str]] = []

    def fake_call(system_prompt: str, user_prompt: str) -> str:
        calls.append((system_prompt, user_prompt))
        return _reviewer_json()

    monkeypatch.setattr("memorymaster.knowledge.skills.llm_provider.call_llm", fake_call)
    first = review_due_skills(service, scopes=["project:memorymaster"], limit=3)
    second = review_due_skills(service, scopes=["project:memorymaster"], limit=3)

    assert first["created"] == 1
    assert first["llm_calls"] == 1
    assert second["considered"] == 0
    assert len(calls) == 1
    assert "untrusted data" in calls[0][0]
    assert str(rule.id) in calls[0][1]


def test_cycle_skill_review_is_default_off_and_budget_bounded(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rule(service)
    monkeypatch.delenv("MEMORYMASTER_SKILL_REVIEW", raising=False)
    monkeypatch.setattr(
        "memorymaster.knowledge.skills.llm_provider.call_llm",
        lambda *_args: pytest.fail("default-off skill review called the LLM"),
    )
    disabled = service.run_cycle(batch_limit=10)
    assert disabled["skill_review"] == {"enabled": False}

    monkeypatch.setenv("MEMORYMASTER_SKILL_REVIEW", "1")
    monkeypatch.setenv("MEMORYMASTER_SKILL_REVIEW_LIMIT", "1")
    monkeypatch.setattr(
        "memorymaster.knowledge.skills.llm_provider.call_llm",
        lambda *_args: _reviewer_json(),
    )
    enabled = service.run_cycle(batch_limit=10)
    assert enabled["skill_review"]["enabled"] is True
    assert enabled["skill_review"]["llm_calls"] == 1
    assert enabled["skill_review"]["created"] == 1


def test_unknown_reviewer_output_is_blocked_once(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rule(service)
    calls = 0

    def unknown(*_args) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"classification": "invented", "payload": {}})

    monkeypatch.setattr("memorymaster.knowledge.skills.llm_provider.call_llm", unknown)
    first = review_due_skills(service, scopes=["project:memorymaster"], limit=1)
    second = review_due_skills(service, scopes=["project:memorymaster"], limit=1)
    assert first["blocked"] == 1
    assert second["considered"] == 0
    assert calls == 1


def test_empty_provider_response_remains_retryable(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rule(service)
    calls = 0

    def empty(*_args) -> str:
        nonlocal calls
        calls += 1
        return ""

    monkeypatch.setattr("memorymaster.knowledge.skills.llm_provider.call_llm", empty)
    first = review_due_skills(service, scopes=["project:memorymaster"], limit=1)
    second = review_due_skills(service, scopes=["project:memorymaster"], limit=1)
    assert first["errors"][0]["error_type"] == "SkillReviewerTransientError"
    assert second["considered"] == 1
    assert calls == 2


def test_reviewer_never_selects_global_scope_implicitly(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    trigger = "handling a global-looking instruction"
    action = "keep it scoped"
    claim = service.ingest(
        **build_rule_fields(trigger, action, "global inference is unsafe"),
        citations=[CitationInput(source="verbatim", locator="global-correction")],
        scope="global",
        source_agent="rule-miner",
    )
    with sqlite3.connect(service.store.db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS rule_stats "
            "(rule_fingerprint TEXT PRIMARY KEY, correction_count INTEGER NOT NULL, "
            "last_mined TEXT NOT NULL, confidence_at_last_mine REAL)"
        )
        conn.execute(
            "INSERT INTO rule_stats(rule_fingerprint, correction_count, last_mined) VALUES (?, 2, ?)",
            (rule_fingerprint(trigger, action), "2026-08-07T00:00:00+00:00"),
        )
    monkeypatch.setattr(
        "memorymaster.knowledge.skills.llm_provider.call_llm",
        lambda *_args: pytest.fail("implicit global rule reached the reviewer"),
    )
    result = review_due_skills(service, limit=5)
    assert result["considered"] == 0
    assert service.store.get_claim(claim.id).status == "candidate"


def test_secret_bearing_skill_payload_is_rejected_before_persistence(
    service: MemoryService,
) -> None:
    rule = _rule(service)
    secret = "ghp_" + "S" * 36
    with pytest.raises(SensitiveMetadataError):
        propose_skill(
            service,
            payload=_payload(expected_output=f"Use {secret}"),
            supporting_claim_ids=[rule.id],
            scope="project:memorymaster",
        )
    assert not [claim for claim in service.store.list_claims(limit=50) if claim.claim_type == "skill"]


def test_repeated_rule_evidence_creates_one_candidate_and_requires_approval(
    service: MemoryService,
) -> None:
    rule = _rule(service, correction_count=2)
    inputs = collect_skill_proposal_inputs(
        service, scope="project:memorymaster", min_corrections=2
    )
    assert [item["claim_id"] for item in inputs] == [rule.id]

    first = propose_skill(
        service,
        payload=_payload(),
        supporting_claim_ids=[rule.id],
        scope="project:memorymaster",
    )
    replay = propose_skill(
        service,
        payload=_payload(),
        supporting_claim_ids=[rule.id],
        scope="project:memorymaster",
    )
    assert first["created"] is True
    assert replay == {"ok": True, "created": False, "claim_id": first["claim_id"], "reason": "duplicate"}

    assert recall_skills(service, "release gate", scope_allowlist=["project:memorymaster"]) == []
    cycle = validator.run(service.store, min_citations=1, min_score=0.0)
    assert cycle["skill_pending_approval"] == 1
    assert service.store.get_claim(first["claim_id"]).status == "candidate"

    approved = approve_skill_candidate(service, first["claim_id"], actor="operator")
    replayed_approval = approve_skill_candidate(service, first["claim_id"], actor="operator")
    assert approved["approved"] is True
    assert replayed_approval["approved"] is False
    assert replayed_approval["reason"] == "already_approved"
    hits = recall_skills(service, "release gate", scope_allowlist=["project:memorymaster"])
    assert [item["claim_id"] for item in hits] == [first["claim_id"]]


def test_rule_evidence_requires_three_independent_root_sessions(service: MemoryService) -> None:
    rule = _rule(service, correction_count=3, root_sessions=2)
    with pytest.raises(SkillValidationError, match="three independent human root sessions"):
        propose_skill(
            service,
            payload=_payload(),
            supporting_claim_ids=[rule.id],
            scope="project:memorymaster",
        )


def test_skill_candidate_inherits_exact_evidence_lineage(service: MemoryService) -> None:
    rule = _rule(service)
    source = service.upsert_external_source(source_type="test", display_name="skill-lineage")
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id="correction-1",
        item_type="text",
        text="The operator corrected this workflow twice.",
    )
    evidence = service.add_evidence_item(
        source_item_id=item.id,
        evidence_type="text",
        text="Run the full release gate before publishing.",
    )
    CaptureRepository(service.store).link_claim_evidence(
        claim_id=rule.id, evidence_item_id=evidence.id
    )

    proposed = propose_skill(
        service,
        payload=_payload(),
        supporting_claim_ids=[rule.id],
        scope="project:memorymaster",
    )
    with service.store.connect() as conn:
        links = conn.execute(
            "SELECT evidence_item_id, role FROM claim_evidence_links WHERE claim_id=?",
            (proposed["claim_id"],),
        ).fetchall()
    assert [(row["evidence_item_id"], row["role"]) for row in links] == [
        (evidence.id, "skill_support")
    ]


def test_cross_scope_support_is_rejected(service: MemoryService) -> None:
    rule = _rule(service)
    with pytest.raises(SkillValidationError, match="outside scope"):
        propose_skill(
            service,
            payload=_payload(),
            supporting_claim_ids=[rule.id],
            scope="project:other",
        )


def test_approved_update_supersedes_without_rewriting_prior_version(
    service: MemoryService,
) -> None:
    rule = _rule(service)
    first = propose_skill(
        service,
        payload=_payload(),
        supporting_claim_ids=[rule.id],
        scope="project:memorymaster",
    )
    approve_skill_candidate(service, first["claim_id"], actor="operator")
    parent = service.store.get_claim(first["claim_id"])
    original_payload = parent.object_value
    approved_replay = propose_skill(
        service,
        payload=_payload(),
        supporting_claim_ids=[rule.id],
        scope="project:memorymaster",
    )
    assert approved_replay["created"] is False
    assert approved_replay["claim_id"] == parent.id

    update_payload = _payload(
        workflow=["Run focused tests.", "Restore a snapshot.", "Run the full release gate."],
        expected_parent_claim_id=parent.id,
        expected_parent_version=parent.version,
    )
    second = propose_skill(
        service,
        payload=update_payload,
        supporting_claim_ids=[rule.id],
        scope="project:memorymaster",
    )
    approve_skill_candidate(service, second["claim_id"], actor="operator")

    old = service.store.get_claim(parent.id)
    new = service.store.get_claim(second["claim_id"])
    assert old.status == "superseded"
    assert old.object_value == original_payload
    assert old.replaced_by_claim_id == new.id
    assert new.status == "confirmed"
    assert new.supersedes_claim_id == old.id
    assert parse_skill(new)["skill_version"] == 2
    integrity = service.store.reconcile_integrity(fix=False)
    assert integrity["summary"]["hash_chain_issues"] == 0
    assert integrity["summary"]["transition_issues"] == 0


def test_reject_archives_candidate_with_audit(service: MemoryService) -> None:
    rule = _rule(service)
    proposed = propose_skill(
        service,
        payload=_payload(),
        supporting_claim_ids=[rule.id],
        scope="project:memorymaster",
    )
    result = reject_skill_candidate(service, proposed["claim_id"], actor="operator", reason="too broad")
    assert result["rejected"] is True
    assert service.store.get_claim(proposed["claim_id"]).status == "archived"
    assert "skill_candidate_rejected" in {
        event.details for event in service.store.list_events(claim_id=proposed["claim_id"], limit=20)
    }


def test_parent_version_race_rolls_back_whole_approval(service: MemoryService) -> None:
    rule = _rule(service)
    first = propose_skill(
        service,
        payload=_payload(),
        supporting_claim_ids=[rule.id],
        scope="project:memorymaster",
    )
    approve_skill_candidate(service, first["claim_id"], actor="operator")
    parent = service.store.get_claim(first["claim_id"])
    second = propose_skill(
        service,
        payload=_payload(
            workflow=["Run focused tests.", "Run restore tests.", "Run the full release gate."],
            expected_parent_claim_id=parent.id,
            expected_parent_version=parent.version,
        ),
        supporting_claim_ids=[rule.id],
        scope="project:memorymaster",
    )
    with service.store.connect() as conn:
        conn.execute("UPDATE claims SET version=version+1 WHERE id=?", (parent.id,))
        conn.commit()

    with pytest.raises(ConcurrentModificationError, match="parent claim"):
        approve_skill_candidate(service, second["claim_id"], actor="operator")
    assert service.store.get_claim(parent.id).status == "confirmed"
    assert service.store.get_claim(second["claim_id"]).status == "candidate"


def test_export_confirmed_skill_is_deterministic_and_staging_bounded(
    service: MemoryService, tmp_path: Path
) -> None:
    rule = _rule(service)
    proposed = propose_skill(
        service,
        payload=_payload(),
        supporting_claim_ids=[rule.id],
        scope="project:memorymaster",
    )
    approve_skill_candidate(service, proposed["claim_id"], actor="operator")
    staging = tmp_path / "memorymaster-staging"

    first = export_confirmed_skills(
        service, staging_root=staging, scope_allowlist=["project:memorymaster"]
    )
    before = Path(first["files"][0]).read_bytes()
    second = export_confirmed_skills(
        service, staging_root=staging, scope_allowlist=["project:memorymaster"]
    )
    after = Path(second["files"][0]).read_bytes()

    assert before == after
    assert Path(first["files"][0]).resolve().is_relative_to(staging.resolve())
    rendered = after.decode("utf-8")
    assert "memorymaster_claim_id:" in rendered
    assert "memorymaster_content_sha256:" in rendered
    assert "memorymaster_skill_version: 1" in rendered
    assert "claim:" in rendered


def test_cli_skill_candidate_approval_recall_and_export(
    service: MemoryService, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rule = _rule(service)
    payload_file = tmp_path / "skill.json"
    payload_file.write_text(json.dumps(_payload()), encoding="utf-8")
    common = ["--json", "--db", str(service.store.db_path), "--workspace", str(tmp_path)]

    assert cli_main(common + [
        "skill-propose", "--input", str(payload_file), "--scope", "project:memorymaster",
        "--supporting-claim-id", str(rule.id),
    ]) == 0
    proposed = json.loads(capsys.readouterr().out)
    assert proposed["created"] is True

    assert cli_main(common + [
        "skill-review", "--claim-id", str(proposed["claim_id"]), "--action", "approve",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["approved"] is True

    assert cli_main(common + [
        "skill-recall", "release gate", "--scope", "project:memorymaster",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == 1

    staging = tmp_path / "cli-staging"
    assert cli_main(common + [
        "skill-export", "--output", str(staging), "--scope", "project:memorymaster",
    ]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["exported"] == 1
    assert Path(exported["files"][0]).is_relative_to(staging)
