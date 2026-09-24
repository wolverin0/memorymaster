"""Dream-originated skill approval must use the authoritative receipt gate."""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.dreaming.source_review import is_dream_claim
from memorymaster.knowledge.skill_schema import build_skill_fields
from memorymaster.knowledge.skills import SkillValidationError, approve_skill_candidate


@pytest.fixture
def service(tmp_path: Path) -> MemoryService:
    result = MemoryService(tmp_path / "dream-skill.db", workspace_root=tmp_path)
    result.init_db()
    return result


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
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


def _ingest_skill(service: MemoryService, payload: dict[str, object], *, source_agent: str):
    return service.ingest(
        **build_skill_fields(payload, supporting_claim_ids=[1]),
        citations=[CitationInput(source="test", locator="skill-gate")],
        scope="project:memorymaster",
        source_agent=source_agent,
    )


def test_dream_skill_approval_without_receipt_preserves_parent_and_candidate(
    service: MemoryService,
) -> None:
    parent = _ingest_skill(service, _payload(), source_agent="skill-reviewer")
    approve_skill_candidate(service, parent.id, actor="operator")
    parent = service.store.get_claim(parent.id)

    candidate = _ingest_skill(
        service,
        _payload(
            title="Updated safe release check",
            workflow=["Run focused tests.", "Restore a snapshot.", "Run the full release gate."],
            expected_parent_claim_id=parent.id,
            expected_parent_version=parent.version,
            skill_version=2,
        ),
        source_agent="dream-worker",
    )
    assert candidate.id != parent.id
    with service.store.connect() as conn:
        assert is_dream_claim(conn, candidate.id)

    with pytest.raises(SkillValidationError, match="Dreaming skill confirmation requires"):
        approve_skill_candidate(service, candidate.id, actor="operator")

    assert service.store.get_claim(parent.id).status == "confirmed"
    assert service.store.get_claim(candidate.id).status == "candidate"
