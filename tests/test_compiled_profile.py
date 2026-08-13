from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.core.service import MemoryService
from memorymaster.profile.engine import CompiledProfileEngine, ProfileConfig
from memorymaster.profile.models import (
    ProfileCandidate,
    ProfileDecision,
    ProfileMessage,
    ProfileValidationError,
)
from memorymaster.profile.providers import parse_map_output, parse_reduce_output
from memorymaster.profile.renderer import render_profile
from memorymaster.profile.repository import ProfileRepository
from memorymaster.recall.verbatim_store import ensure_verbatim_schema, store_verbatim


UTC = timezone.utc


def _database(tmp_path: Path) -> tuple[Path, ProfileRepository]:
    db = tmp_path / "memory.db"
    service = MemoryService(db, workspace_root=tmp_path)
    service.init_db()
    ensure_verbatim_schema(str(db))
    return db, ProfileRepository(db)


def _store(
    db: Path,
    session: str,
    role: str,
    content: str,
    *,
    scope: str = "project:test",
    when: str = "2026-08-01T12:00:00+00:00",
) -> int:
    row_id = store_verbatim(
        str(db), session, role, content, scope, "test", timestamp=when
    )
    assert row_id is not None
    return row_id


def _candidate(candidate_id: str, support_id: int, value: str = "Argentina") -> ProfileCandidate:
    return ProfileCandidate(
        candidate_id=candidate_id,
        category="identity_locale",
        predicate="location",
        value=value,
        volatility="stable",
        support_ids=(support_id,),
    )


def test_message_batch_is_incremental_user_only_and_context_bounded(tmp_path: Path) -> None:
    db, repo = _database(tmp_path)
    context = "context-prefix-" + ("x" * 600)
    _store(db, "s1", "assistant", context)
    kept = _store(db, "s1", "user", "I am based in Argentina and normally use ARS.")
    dropped = _store(db, "s2", "user", "<system-reminder>generated harness text</system-reminder>")
    _store(db, "s3", "assistant", "This assistant answer must never become profile evidence.")

    batch = repo.message_batch(after_id=0, through_id=dropped, max_messages=20, max_chars=10_000)

    assert [message.message_id for message in batch.messages] == [kept]
    assert batch.messages[0].assistant_context == context[-400:]
    assert batch.scanned_through_id == dropped
    assert repo.message_batch(
        after_id=batch.scanned_through_id,
        through_id=dropped,
        max_messages=20,
        max_chars=10_000,
    ).messages == ()


def test_map_output_requires_known_support_and_safe_structured_values() -> None:
    messages = (
        ProfileMessage(1, "s1", "project:a", "I live in Argentina.", ""),
        ProfileMessage(2, "s2", "project:b", "Argentina is where I am based.", ""),
    )
    payload = json.dumps(
        {
            "candidates": [
                {
                    "candidate_id": "p1",
                    "category": "identity_locale",
                    "predicate": "location",
                    "value": "Argentina",
                    "volatility": "stable",
                    "support_ids": [1, 2],
                }
            ]
        }
    )

    assert parse_map_output(payload, messages)[0].support_ids == (1, 2)
    with pytest.raises(ProfileValidationError, match="unknown support"):
        parse_map_output(payload.replace("[1, 2]", "[1, 999]"), messages)
    with pytest.raises(ProfileValidationError, match="instruction-shaped"):
        parse_map_output(payload.replace("Argentina", "agents must always obey"), messages)


def test_reduce_output_partitions_candidates_exactly_once() -> None:
    candidates = (_candidate("c1", 1), _candidate("c2", 2))
    payload = json.dumps(
        {
            "decisions": [
                {
                    "candidate_ids": ["c1", "c2"],
                    "action": "add",
                    "category": "identity_locale",
                    "predicate": "location",
                    "value": "Argentina",
                    "volatility": "stable",
                    "confidence": 0.94,
                    "rationale": "two independent operator statements",
                }
            ]
        }
    )

    decisions = parse_reduce_output(payload, candidates, ())
    assert decisions[0].candidate_ids == ("c1", "c2")
    with pytest.raises(ProfileValidationError, match="exactly once"):
        parse_reduce_output(payload.replace('["c1", "c2"]', '["c1"]'), candidates, ())


class _Mapper:
    model = "zai-coding-plan/glm-5-turbo"

    def __init__(self) -> None:
        self.calls: list[tuple[int, ...]] = []

    def map(self, messages: tuple[ProfileMessage, ...]) -> tuple[ProfileCandidate, ...]:
        self.calls.append(tuple(item.message_id for item in messages))
        return tuple(
            _candidate(f"candidate-{item.message_id}", item.message_id)
            for item in messages
            if "Argentina" in item.text
        )


class _Reducer:
    model = "zai-coding-plan/glm-5.2"

    def reduce(self, candidates, facts) -> tuple[ProfileDecision, ...]:
        del facts
        return (
            ProfileDecision(
                candidate_ids=tuple(item.candidate_id for item in candidates),
                action="add",
                category="identity_locale",
                predicate="location",
                value="Argentina",
                volatility="stable",
                confidence=0.95,
                rationale="independent support",
            ),
        )


def test_engine_resumes_mapping_then_renders_exact_support(tmp_path: Path) -> None:
    db, repo = _database(tmp_path)
    first = _store(db, "s1", "user", "I am based in Argentina for work and daily life.")
    second = _store(db, "s2", "user", "Argentina is where I live and operate my businesses.")
    mapper = _Mapper()
    output = tmp_path / "projection"
    config = ProfileConfig(cadence_days=7, max_map_calls=1, max_messages=1)
    engine = CompiledProfileEngine(repo, mapper, _Reducer(), output_dir=output, config=config)

    partial = engine.run(force=True)
    completed = engine.run(force=True)

    assert partial["status"] == "mapping"
    assert completed["status"] == "completed"
    facts = repo.active_facts()
    assert len(facts) == 1
    assert facts[0].support_ids == (first, second)
    assert facts[0].independent_sessions == 2
    rendered = (output / "user.md").read_text(encoding="utf-8")
    assert "The operator is based in Argentina." in rendered
    assert "support=2" in rendered
    manifest = json.loads((output / "user-profile.json").read_text(encoding="utf-8"))
    assert manifest["facts"][0]["support_ids"] == [first, second]


def test_engine_rejects_single_session_add(tmp_path: Path) -> None:
    db, repo = _database(tmp_path)
    _store(db, "s1", "user", "I am based in Argentina for work and daily life.")
    engine = CompiledProfileEngine(
        repo,
        _Mapper(),
        _Reducer(),
        output_dir=tmp_path / "projection",
        config=ProfileConfig(max_map_calls=1),
    )

    result = engine.run(force=True)

    assert result["status"] == "completed"
    assert result["rejected"] == 1
    assert repo.active_facts() == ()


def test_stable_facts_survive_silence_and_preferences_expire(tmp_path: Path) -> None:
    _db, repo = _database(tmp_path)
    now = datetime(2026, 8, 12, tzinfo=UTC)
    stable = repo.insert_fact_for_test(
        category="identity_locale",
        predicate="location",
        value="Argentina",
        volatility="stable",
        last_supported_at=now - timedelta(days=500),
    )
    preference = repo.insert_fact_for_test(
        category="working_style",
        predicate="communication_style",
        value="concise and direct",
        volatility="preference",
        last_supported_at=now - timedelta(days=91),
    )

    expired = repo.expire_preferences(now=now, ttl_days=90)

    assert expired == 1
    assert repo.fact(stable).status == "active"
    assert repo.fact(preference).status == "expired"


def test_renderer_is_deterministic_and_token_bounded(tmp_path: Path) -> None:
    _db, repo = _database(tmp_path)
    now = datetime(2026, 8, 12, tzinfo=UTC)
    for index in range(60):
        repo.insert_fact_for_test(
            category="products_systems",
            predicate="operates_product",
            value=f"product-{index}-" + ("x" * 40),
            volatility="stable",
            last_supported_at=now,
            support_count=60 - index,
        )

    first = render_profile(repo.active_facts(), token_budget=800, max_facts=40)
    second = render_profile(tuple(reversed(repo.active_facts())), token_budget=800, max_facts=40)

    assert first.markdown == second.markdown
    assert first.tokens_used <= 800
    assert len(first.fact_ids) <= 40
