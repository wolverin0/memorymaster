"""Intent-anchored tests for the P3 rule-mining steward phase in run_cycle.

The phase makes correction-mining automatic: run_cycle calls
``rule_miner.mine_rules`` as an additional steward phase, gated DEFAULT OFF by
``MEMORYMASTER_STEWARD_RULE_MINING``. These tests anchor on WHY the design is
safe, not just that the wiring exists:

1. flag OFF (default) -> run_cycle does NOT call mine_rules at all (zero LLM
   spend / zero behavior change vs. today);
2. flag ON + a planted verbatim correction -> a rule candidate is mined and the
   cycle result carries the mining stats under result['rule_mining'];
3. a mine_rules failure does NOT crash run_cycle — the other steward phases
   still run and result['rule_mining'] carries an error marker;
4. REGRESSION: with the flag off, a normal cycle still extracts/validates/decays
   exactly as before (the new phase is purely additive when disabled).
"""
from __future__ import annotations

import sqlite3

import pytest

from memorymaster.core import llm_provider
from memorymaster.knowledge import rule_miner
from memorymaster.knowledge.rules import is_rule, parse_rule
from memorymaster.core.service import MemoryService


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

_RULE_JSON = (
    '{"trigger": "hardcoding a path", '
    '"action": "use an env var instead", '
    '"rationale": "paths differ per machine"}'
)

# An assistant turn followed by a user CORRECTION (carries keyword "no, don't").
_PAIR = [
    (1, "s1", "assistant", "I added the path /etc/foo to the config file directly so it loads."),
    (2, "s1", "user", "no, don't hardcode the path like that, use an env var instead please."),
]


def _create_verbatim(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(_VERBATIM_DDL)
    conn.execute("CREATE VIRTUAL TABLE verbatim_fts USING fts5(content)")
    conn.commit()
    conn.close()


def _seed(db_path: str, rows) -> None:
    conn = sqlite3.connect(db_path)
    for rid, session, role, content in rows:
        conn.execute(
            """INSERT INTO verbatim_memories
               (id, session_id, role, content, scope, timestamp, source_agent)
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
    """A service over a fresh DB with verbatim tables and a stubbed LLM.

    Yields ``(svc, holder)``. ``holder['responses']`` is popped by the stub
    provider (falls back to "{}"); ``holder['calls']`` counts invocations.
    The flag starts UNSET so each test opts in explicitly.
    """
    db = tmp_path / "mm.db"
    svc = MemoryService(db, workspace_root=tmp_path)
    svc.init_db()
    _create_verbatim(str(db))

    # Clean budget + flag env so unset == default (off / unlimited).
    for var in (
        "MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE",
        "MEMORYMASTER_MAX_TOKENS_PER_CYCLE",
        "MEMORYMASTER_MAX_PROVIDER_FAILURES_PER_CYCLE",
        "MEMORYMASTER_LLM_FALLBACK_PROVIDER",
        "MEMORYMASTER_STEWARD_RULE_MINING",
        "MEMORYMASTER_STEWARD_RULE_MINING_LIMIT",
    ):
        monkeypatch.delenv(var, raising=False)

    holder = {"responses": [], "calls": 0}

    def _stub(prompt, text):  # noqa: ARG001 — provider signature
        holder["calls"] += 1
        resp = holder["responses"]
        return resp.pop(0) if resp else "{}"

    monkeypatch.setitem(llm_provider._PROVIDERS, "claude_cli", _stub)
    return svc, holder


def test_flag_off_does_not_call_mine_rules(env, monkeypatch):
    """DEFAULT OFF invariant: with the flag unset, run_cycle must never invoke
    mine_rules — proving zero new LLM spend and no behavior change vs. today.
    Anchored on the call count, not on stats shape, so it stays true even if
    the result dict format changes."""
    svc, _holder = env
    _seed(str(svc.store.db_path), _PAIR)

    calls = {"n": 0}

    def _spy(*args, **kwargs):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(rule_miner, "mine_rules", _spy)

    result = svc.run_cycle()

    assert calls["n"] == 0, "mine_rules must NOT be called when the flag is off"
    # The phase still reports it is disabled, without doing any work.
    assert result["rule_mining"] == {"enabled": False}


def test_flag_on_mines_rule_and_reports_stats(env, monkeypatch):
    """Flag ON + a planted correction -> a rule candidate is mined and the cycle
    result surfaces the mining stats under result['rule_mining']. Anchored on
    the actual ingested rule (the user-visible outcome), not just the stats."""
    svc, holder = env
    monkeypatch.setenv("MEMORYMASTER_STEWARD_RULE_MINING", "1")
    _seed(str(svc.store.db_path), _PAIR)
    holder["responses"] = [_RULE_JSON]

    result = svc.run_cycle()

    mining = result["rule_mining"]
    assert mining["enabled"] is True
    assert mining["ingested"] == 1, "the planted correction should mine one rule"
    assert mining["candidates"] == 1
    assert mining["llm_calls"] == 1

    rules = _candidate_rules(svc)
    assert len(rules) == 1
    assert rules[0]["trigger"] == "hardcoding a path"
    assert rules[0]["action"] == "use an env var instead"


def test_mine_rules_failure_does_not_crash_cycle(env, monkeypatch):
    """Resilience invariant: a mine_rules exception must be isolated so the rest
    of run_cycle still completes. We assert the OTHER phases ran (proving the
    cycle did not abort) and that result['rule_mining'] carries an error marker
    instead of propagating."""
    svc, _holder = env
    monkeypatch.setenv("MEMORYMASTER_STEWARD_RULE_MINING", "1")
    _seed(str(svc.store.db_path), _PAIR)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated mining failure")

    monkeypatch.setattr(rule_miner, "mine_rules", _boom)

    result = svc.run_cycle()  # must NOT raise

    assert result["rule_mining"] == {"enabled": True, "error": "simulated mining failure"}
    # The cycle did not abort: phases before AND after the mining phase ran.
    assert "extractor" in result
    assert "validator" in result
    assert "decay" in result
    assert "integrity" in result  # runs after the cycle_scope block


def test_regression_flag_off_normal_cycle_unchanged(env, monkeypatch):
    """REGRESSION: with the flag off (default), a normal cycle still runs the
    full steward pipeline — extractor, validator, decay — exactly as before.
    The new phase is purely additive when disabled, and makes zero LLM calls."""
    svc, holder = env
    _seed(str(svc.store.db_path), _PAIR)

    result = svc.run_cycle()

    # All pre-existing phases present and the cycle completed normally.
    for phase in ("policy", "extractor", "dedupe", "deterministic", "validator", "decay", "compactor"):
        assert phase in result, f"expected steward phase {phase!r} to still run"
    assert result["rule_mining"] == {"enabled": False}
    # No rule candidates were mined and the mining LLM made no calls.
    assert _candidate_rules(svc) == []
    assert holder["calls"] == 0
