"""Tests for the verbatim rule-miner (v3.21.0-R1b).

The miner scans ``verbatim_memories`` for correction-signaled user turns,
asks an LLM to distill each into a rule, and ingests rule-shaped claims.
These tests plant synthetic verbatim rows, stub the LLM provider (so the
real ``call_llm`` budget gate still runs), and assert mining behavior:
extraction, idempotency, the keyword pre-filter, budget abort, watermark
resume, and sensitive-rule drop.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from memorymaster import llm_provider, rule_miner
from memorymaster.rules import is_rule, parse_rule
from memorymaster.service import MemoryService


_VERBATIM_DDL = """
CREATE TABLE verbatim_memories (
    id INTEGER PRIMARY KEY,
    session_id TEXT,
    role TEXT,
    content TEXT,
    scope TEXT,
    timestamp TEXT,
    source_agent TEXT,
    embedding_synced INTEGER DEFAULT 0
)
"""


def _create_verbatim(db_path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(_VERBATIM_DDL)
    conn.execute("CREATE VIRTUAL TABLE verbatim_fts USING fts5(content)")
    conn.commit()
    conn.close()


def _seed(db_path, rows) -> None:
    """rows: iterable of (id, session_id, role, content)."""
    conn = sqlite3.connect(db_path)
    for rid, session, role, content in rows:
        conn.execute(
            """INSERT INTO verbatim_memories (id, session_id, role, content, scope, timestamp, source_agent)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (rid, session, role, content, "project:test", "2026-05-20T00:00:00Z", "stop-hook"),
        )
        conn.execute("INSERT INTO verbatim_fts(rowid, content) VALUES (?, ?)", (rid, content))
    conn.commit()
    conn.close()


def _candidate_rules(svc) -> list[dict]:
    claims = svc.store.find_by_status("candidate", limit=100, include_citations=False)
    return [parse_rule(c) for c in claims if is_rule(c)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A DB with both claims schema and verbatim tables, plus a stubbed LLM.

    Yields ``(db_path, svc, holder)`` where ``holder["responses"]`` is a list
    the stub provider pops from (falls back to "{}" when empty) and
    ``holder["calls"]`` counts provider invocations.
    """
    db = tmp_path / "mm.db"
    svc = MemoryService(db, workspace_root=tmp_path)
    svc.init_db()
    _create_verbatim(db)

    # Clean budget/fallback env so unset = unlimited unless a test opts in.
    for var in (
        "MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE",
        "MEMORYMASTER_MAX_TOKENS_PER_CYCLE",
        "MEMORYMASTER_MAX_PROVIDER_FAILURES_PER_CYCLE",
        "MEMORYMASTER_LLM_FALLBACK_PROVIDER",
    ):
        monkeypatch.delenv(var, raising=False)

    holder = {"responses": [], "calls": 0}

    def _stub(prompt, text):  # noqa: ARG001 — provider signature
        holder["calls"] += 1
        resp = holder["responses"]
        return resp.pop(0) if resp else "{}"

    # Register under the provider the miner selects, so the real call_llm
    # (budget gate + fallback logic) runs and dispatches to the stub.
    monkeypatch.setitem(llm_provider._PROVIDERS, "claude_cli", _stub)
    return str(db), svc, holder


_RULE_JSON = json.dumps(
    {"trigger": "hardcoding a path", "action": "use an env var instead", "rationale": "paths differ per machine"}
)

_PAIR = [
    (1, "s1", "assistant", "I added the path /etc/foo to the config file directly so it loads."),
    (2, "s1", "user", "no, don't hardcode the path like that, use an env var instead please."),
]


def test_mine_plants_rule(env):
    db, svc, holder = env
    _seed(db, _PAIR)
    holder["responses"] = [_RULE_JSON]

    stats = rule_miner.mine_rules(db, svc, provider="claude_cli")

    assert stats["candidates"] == 1
    assert stats["llm_calls"] == 1
    assert stats["ingested"] == 1
    assert stats["last_id"] == 2

    rules = _candidate_rules(svc)
    assert len(rules) == 1
    assert rules[0]["trigger"] == "hardcoding a path"
    assert rules[0]["action"] == "use an env var instead"


def test_idempotent_rerun(env):
    db, svc, holder = env
    _seed(db, _PAIR)
    holder["responses"] = [_RULE_JSON]
    first = rule_miner.mine_rules(db, svc, provider="claude_cli")
    assert first["ingested"] == 1

    # Re-scan from the start: same window -> idempotency key hit, no new claim.
    holder["responses"] = [_RULE_JSON]
    second = rule_miner.mine_rules(db, svc, provider="claude_cli", reset=True)
    assert second["candidates"] == 1
    assert second["ingested"] == 0
    assert second["duplicates"] == 1
    assert len(_candidate_rules(svc)) == 1


def test_no_correction_no_rule(env):
    db, svc, holder = env
    _seed(db, _PAIR)
    holder["responses"] = ["{}"]  # LLM says: not a correction

    stats = rule_miner.mine_rules(db, svc, provider="claude_cli")

    assert stats["ingested"] == 0
    assert stats["skipped"] == 1
    assert stats["last_id"] == 2  # watermark still advances past the row
    assert _candidate_rules(svc) == []


def test_keyword_prefilter_skips_non_corrections(env):
    db, svc, holder = env
    _seed(db, [
        (1, "s1", "assistant", "I refactored the helper and split it into two functions."),
        (2, "s1", "user", "thanks, that looks great, please ship it whenever ready."),
    ])

    stats = rule_miner.mine_rules(db, svc, provider="claude_cli")

    assert stats["candidates"] == 0
    assert holder["calls"] == 0  # the LLM was never consulted
    assert stats["ingested"] == 0


def test_budget_cap_aborts(env, monkeypatch):
    db, svc, holder = env
    monkeypatch.setenv("MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE", "1")
    _seed(db, [
        (1, "s1", "assistant", "I committed straight to main without opening a PR for the change."),
        (2, "s1", "user", "no, don't commit to main directly, open a PR instead next time."),
        (3, "s2", "assistant", "I deleted the old migration file to clean things up a bit."),
        (4, "s2", "user", "no, don't delete migrations, they are immutable once applied."),
        (5, "s3", "assistant", "I bumped the dependency to the latest major version directly."),
        (6, "s3", "user", "no, don't auto-bump majors, pin them instead to avoid breakage."),
    ])
    holder["responses"] = [_RULE_JSON, _RULE_JSON, _RULE_JSON]

    stats = rule_miner.mine_rules(db, svc, provider="claude_cli")

    assert stats["llm_calls"] == 1
    assert stats["ingested"] == 1
    assert stats["aborted_reason"] == "calls_exhausted"
    assert stats["last_id"] == 2  # watermark at the last fully-processed row


def test_watermark_resume(env):
    db, svc, holder = env
    _seed(db, [
        (1, "s1", "assistant", "I committed straight to main without opening a PR for the change."),
        (2, "s1", "user", "no, don't commit to main directly, open a PR instead next time."),
        (3, "s2", "assistant", "I deleted the old migration file to clean things up a bit."),
        (4, "s2", "user", "no, don't delete migrations, they are immutable once applied."),
    ])
    rule_a = json.dumps({"trigger": "committing", "action": "open a PR", "rationale": "review"})
    rule_b = json.dumps({"trigger": "migrations", "action": "never delete them", "rationale": "immutable"})
    holder["responses"] = [rule_a, rule_b]

    first = rule_miner.mine_rules(db, svc, provider="claude_cli", limit=1)
    assert first["candidates"] == 1
    assert first["last_id"] == 2

    second = rule_miner.mine_rules(db, svc, provider="claude_cli")
    assert second["candidates"] == 1  # only the row after the watermark
    assert second["ingested"] == 1
    assert second["last_id"] == 4

    actions = sorted(r["action"] for r in _candidate_rules(svc))
    assert actions == ["never delete them", "open a PR"]


def test_sensitive_rule_dropped(env):
    db, svc, holder = env
    _seed(db, _PAIR)
    leak = "ghp_" + "A" * 36  # GitHub token shape — caught by the sensitivity filter
    holder["responses"] = [json.dumps(
        {"trigger": "auth", "action": f"use the token {leak}", "rationale": "ci"}
    )]

    stats = rule_miner.mine_rules(db, svc, provider="claude_cli")

    assert stats["ingested"] == 0
    assert stats["skipped"] == 1
    assert _candidate_rules(svc) == []


def test_rejects_postgres_dsn(env):
    _, svc, _ = env
    with pytest.raises(ValueError, match="SQLite-only"):
        rule_miner.mine_rules("postgresql://localhost/mm", svc)
