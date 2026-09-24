"""Skill retrieval must rank the authorized catalog, not a generic claim window."""
from __future__ import annotations

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.knowledge.skills import approve_skill_candidate, build_skill_fields, recall_skills


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORYMASTER_JEV_SKILLS_ENABLED", raising=False)
    result = MemoryService(tmp_path / "catalog.db", workspace_root=tmp_path)
    result.init_db()
    return result


def skill(service, slug="release-check", *, scope="project:test", **kwargs):
    payload = {
        "schema": "personal-skill-v1", "slug": slug, "title": f"Release check {slug}",
        "when_to_use": "Before preparing a release.", "when_not_to_use": "For unrelated work.",
        "inputs": ["candidate"], "prerequisites": ["disposable database"],
        "workflow": ["Run the release tests."], "decision_rules": ["Stop on failure."],
        "expected_output": "Release evidence", "validation": ["Check test results."],
        "pitfalls": ["Do not ignore failures."], "recovery": ["Restore the candidate."],
        "quality_scores": dict.fromkeys(
            ["recurrence", "reusability", "executability", "validation", "safety"], 16
        ),
    }
    claim = service.ingest(
        **build_skill_fields(payload, supporting_claim_ids=[1]),
        citations=[CitationInput(source="test", locator="fixture")],
        scope=scope, source_agent=kwargs.pop("source_agent", "fixture"), **kwargs,
    )
    approve_skill_candidate(service, claim.id, actor="test-operator")
    return service.store.get_claim(claim.id)


def test_skill_not_displaced_by_ordinary_claims(service):
    wanted = skill(service)
    for index in range(70):
        claim = service.ingest(
            text=f"release {index}", confidence=1.0, scope="project:test",
            citations=[CitationInput(source="test")],
        )
        with service.store.connect() as conn:
            conn.execute("UPDATE claims SET status='confirmed' WHERE id=?", (claim.id,))
    rows = recall_skills(service, "release", scope_allowlist=["project:test"], limit=1)
    assert [row["claim_id"] for row in rows] == [wanted.id]
    assert rows[0]["citations"][0]["locator"] == "fixture"


def test_skill_catalog_excludes_temporally_invalid_and_replaced_claims(service):
    wanted = skill(service, "current")
    expired = skill(service, "expired")
    future = skill(service, "future")
    replaced = skill(service, "replaced")
    with service.store.connect() as conn:
        conn.execute("UPDATE claims SET valid_until='2020-01-01T00:00:00+00:00' WHERE id=?", (expired.id,))
        conn.execute("UPDATE claims SET valid_from='2099-01-01T00:00:00+00:00' WHERE id=?", (future.id,))
        conn.execute("UPDATE claims SET replaced_by_claim_id=? WHERE id=?", (wanted.id, replaced.id))
    rows = recall_skills(service, "release", scope_allowlist=["project:test"])
    assert [row["claim_id"] for row in rows] == [wanted.id]


def test_nonpositive_limit_is_empty(service):
    skill(service)
    assert recall_skills(service, "release", limit=0) == []


def test_no_matching_skill_does_not_emit_arbitrary_catalog_entry(service):
    skill(service)
    assert recall_skills(service, "underwater basketweaving") == []


def test_catalog_pages_past_unrelated_skills_before_ranking(service):
    # The target need not be in the first generic claim page, nor the first
    # page of skills. All candidates are ranked before the requested top one.
    for index in range(260):
        skill(service, f"other-{index}")
    wanted = skill(service, "unique-target")
    rows = recall_skills(service, "unique-target", limit=1)
    assert [row["claim_id"] for row in rows] == [wanted.id]


def test_jev_can_select_skill_with_no_lexical_overlap(service, monkeypatch):
    wanted = skill(service)
    seen = []

    def select(query, catalog, **_):
        seen.extend(item["claim_id"] for item in catalog)
        return [wanted.id]

    monkeypatch.setattr("memorymaster.knowledge.jev_selector.select_skill_ids", select)
    rows = recall_skills(service, "Ship the application safely", scope_allowlist=["project:test"], limit=1)
    assert seen == [wanted.id]
    assert [row["claim_id"] for row in rows] == [wanted.id]


@pytest.mark.parametrize("decision", [None, [999999], [True], [1, 1]])
def test_invalid_or_uncertain_selection_preserves_lexical_result(service, monkeypatch, decision):
    wanted = skill(service)
    monkeypatch.setattr("memorymaster.knowledge.jev_selector.select_skill_ids", lambda *_, **__: decision)
    assert [row["claim_id"] for row in recall_skills(service, "release")] == [wanted.id]


def test_confident_none_does_not_fall_back_to_unwanted_skill(service, monkeypatch):
    skill(service)
    monkeypatch.setattr("memorymaster.knowledge.jev_selector.select_skill_ids", lambda *_, **__: [])
    assert recall_skills(service, "release") == []


def test_revalidate_lifecycle_after_provider_response(service, monkeypatch):
    wanted = skill(service)

    def select(*_, **__):
        with service.store.connect() as conn:
            conn.execute("UPDATE claims SET status='archived' WHERE id=?", (wanted.id,))
        return [wanted.id]

    monkeypatch.setattr("memorymaster.knowledge.jev_selector.select_skill_ids", select)
    assert recall_skills(service, "release") == []
    assert service.store.get_claim(wanted.id).status == "archived"


def test_unauthorized_and_inactive_skills_never_reach_selector(service, monkeypatch):
    tenant = MemoryService(
        service.store.db_path, workspace_root=service.workspace_root,
        tenant_id="tenant-a", require_tenant=True, principal="reader",
        allowed_scopes=["project:test"],
    )
    writer = MemoryService(service.store.db_path, tenant_id="tenant-a")
    other_tenant = MemoryService(service.store.db_path, tenant_id="tenant-b")
    wanted = skill(tenant, "allowed", source_agent="reader")
    skill(writer, "private-other", visibility="private", source_agent="other")
    skill(writer, "other-scope", scope="project:elsewhere")
    skill(other_tenant, "other-tenant")
    for status in ["candidate", "stale", "conflicted", "superseded", "archived"]:
        excluded = skill(writer, f"status-{status}")
        with writer.store.connect() as conn:
            conn.execute("UPDATE claims SET status=? WHERE id=?", (status, excluded.id))
    sensitive = skill(writer, "sensitive")
    with writer.store.connect() as conn:
        conn.execute("UPDATE claims SET text='[REDACTED:secret]' WHERE id=?", (sensitive.id,))
    seen = []

    def select(_, catalog, **__):
        seen.extend(item["claim_id"] for item in catalog)
        return [wanted.id]

    monkeypatch.setattr("memorymaster.knowledge.jev_selector.select_skill_ids", select)
    rows = recall_skills(tenant, "release")
    assert seen == [wanted.id]
    assert [row["claim_id"] for row in rows] == [wanted.id]
    with pytest.raises(PermissionError):
        recall_skills(tenant, "release", scope_allowlist=["project:elsewhere"])


def test_sensitive_query_and_private_catalog_never_leave_process(service, monkeypatch):
    wanted = skill(service, visibility="private")
    monkeypatch.setattr(
        "memorymaster.knowledge.jev_selector.select_skill_ids",
        lambda *_, **__: pytest.fail("private material reached external selector"),
    )
    assert [r["claim_id"] for r in recall_skills(service, "release")] == [wanted.id]
    with service.store.connect() as conn:
        conn.execute("UPDATE claims SET visibility='public' WHERE id=?", (wanted.id,))
    recall_skills(service, "release password=SuperSecretExample123!")


def test_public_recall_delivers_selected_skill_and_respects_retirement(service, monkeypatch):
    from memorymaster.public.v1 import forget, recall

    wanted = skill(service)
    monkeypatch.setattr("memorymaster.knowledge.jev_selector.select_skill_ids", lambda *_, **__: [wanted.id])
    arguments = dict(
        db=service.store.db_path, workspace=service.workspace_root,
        scope_allowlist=["project:test"], include_skills=True,
        retrieval_mode="legacy", token_budget=1200,
    )
    receipt = recall("release", **arguments)
    assert [item["claim_id"] for item in receipt.skills] == [wanted.id]
    assert "APPROVED SKILLS" in receipt.output
    assert receipt.tokens_used <= receipt.token_budget
    assert receipt.skills[0]["citations"][0]["locator"] == "fixture"
    forget(claim_id=wanted.id, apply=True, db=service.store.db_path, workspace=service.workspace_root)
    assert recall("release", **arguments).skills == ()


def test_read_only_skill_catalog_uses_same_authorized_selection(service, monkeypatch):
    wanted = skill(service)
    readonly = MemoryService(service.store.db_path, workspace_root=service.workspace_root, read_only=True)
    monkeypatch.setattr(readonly, "_record_accesses", lambda *_args, **_kwargs: None)
    assert [row["claim_id"] for row in recall_skills(readonly, "release")] == [wanted.id]


def test_sqlite_only_catalog_fails_closed_for_unsupported_store():
    from types import SimpleNamespace
    from memorymaster.knowledge.skills import SkillValidationError, _authorized_skill_catalog

    with pytest.raises(SkillValidationError, match="SQLite-only"):
        _authorized_skill_catalog(SimpleNamespace(store=SimpleNamespace(db_path="postgresql://unused")), None)


@pytest.mark.parametrize("timeout_on_verification", [False, True])
def test_real_selector_adapter_integrates_with_sqlite_and_http_fallback(
    service, monkeypatch, tmp_path, timeout_on_verification,
):
    from memorymaster.decisions import transport as transport_module
    from memorymaster.decisions.transport import TransportResult, validate_response

    wanted = skill(service)
    option = f"skill:{wanted.id}"
    calls = []
    monkeypatch.setenv("MEMORYMASTER_JEV_SKILLS_ENABLED", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-not-a-real-key")
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(tmp_path / "decisions.db"))

    class OfflineTransport:  # in-process stand-in for the pinned System One client
        def __init__(self, _key, **_kwargs):
            pass

        def send(self, payload, *, expected, timeout_s, max_retries=0):
            calls.append(payload)
            if len(calls) == 2 and timeout_on_verification:
                return TransportResult(None, None, 900, 1, "timeout")
            answers = {
                "skills.which_skill": {
                    "type": "choice", "choice": option, "confidence": 0.95,
                    "probabilities": {option: 0.99, "none": 0.01},
                },
            }
            if len(calls) == 1:
                answers["skills.needs_procedure"] = {"type": "noul", "noul": 0.9}
            else:
                answers[f"skills.fits::claim:{wanted.id}"] = {"type": "noul", "noul": 0.95}
            data = {"model": "jev-1.13.0", "usage": {"input_tokens": 100, "output_tokens": 0}, "answers": answers}
            validated = validate_response(data, expected)
            return TransportResult(200, data, 40, 1, "ok", model_served=validated.model_served,
                                   tokens_in=100, answers=validated.answers)

        def close(self):
            pass

    monkeypatch.setattr(transport_module, "HttpxTransport", OfflineTransport)
    rows = recall_skills(service, "release", scope_allowlist=["project:test"])
    assert [row["claim_id"] for row in rows] == [wanted.id]
    assert len(calls) == 2
    assert all(call["model"] == "jev-1.13.0" for call in calls)
    assert rows[0]["citations"][0]["locator"] == "fixture"
    assert service.store.get_claim(wanted.id).status == "confirmed"
