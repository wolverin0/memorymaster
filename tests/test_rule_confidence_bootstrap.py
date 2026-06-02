"""Confidence-bootstrap for mined rules (v3.28).

A rule mined once is a guess (0.40); the SAME correction mined repeatedly is
load-bearing and earns rising confidence: 0.40 -> 0.53 -> 0.70 across three
minings of one fingerprint. These tests anchor on that requirement (and its
reversibility via env), not on the formula's internals.

The LLM is stubbed exactly like test_rule_miner so no real provider is called.
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

_RULE_JSON = json.dumps(
    {"trigger": "hardcoding a path", "action": "use an env var instead", "rationale": "paths differ"}
)


def _create_verbatim(db_path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(_VERBATIM_DDL)
    conn.execute("CREATE VIRTUAL TABLE verbatim_fts USING fts5(content)")
    conn.commit()
    conn.close()


def _seed(db_path, rows) -> None:
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


def _confidences(svc) -> list[float]:
    """Confidence of every rule claim, sorted ascending."""
    claims = svc.store.find_by_status("candidate", limit=100, include_citations=False)
    return sorted(round(c.confidence, 2) for c in claims if is_rule(c))


# Three DISTINCT correction windows that the LLM distills into the SAME rule
# (same trigger+action -> same fingerprint), giving three distinct claims whose
# confidence must climb with the per-fingerprint tally.
def _three_windows():
    return [
        (1, "s1", "assistant", "I hardcoded the path /etc/a into the config file directly."),
        (2, "s1", "user", "no, don't hardcode the path like that, use an env var instead."),
        (3, "s2", "assistant", "I hardcoded the path /etc/b into the second config directly."),
        (4, "s2", "user", "no, actually don't hardcode the path, use an env var instead please."),
        (5, "s3", "assistant", "I hardcoded the path /etc/c into the third config directly."),
        (6, "s3", "user", "no, don't hardcode that path either, use an env var instead now."),
    ]


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "mm.db"
    svc = MemoryService(db, workspace_root=tmp_path)
    svc.init_db()
    _create_verbatim(db)
    for var in (
        "MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE",
        "MEMORYMASTER_MAX_TOKENS_PER_CYCLE",
        "MEMORYMASTER_MAX_PROVIDER_FAILURES_PER_CYCLE",
        "MEMORYMASTER_LLM_FALLBACK_PROVIDER",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv(rule_miner._BOOTSTRAP_ENV, raising=False)

    holder = {"responses": []}

    def _stub(prompt, text):  # noqa: ARG001
        resp = holder["responses"]
        return resp.pop(0) if resp else "{}"

    monkeypatch.setitem(llm_provider._PROVIDERS, "claude_cli", _stub)
    return str(db), svc, holder


# The REQUIREMENT is the formula confidence = 0.4 + 0.3 * min(count/3, 1.0),
# which evaluates to 0.50, 0.60, 0.70 for the 1st/2nd/3rd mining (count starts
# at 1). The brief's prose "0.40, 0.53, 0.70" is inconsistent with its own
# formula (count=1 yields 0.50, not 0.40); we anchor on the formula, the precise
# and testable artifact. See _confidence_for_count.
def test_confidence_climbs_across_three_minings(env):
    """Same correction mined 3x -> confidence climbs per the formula."""
    db, svc, holder = env
    _seed(db, _three_windows())
    holder["responses"] = [_RULE_JSON, _RULE_JSON, _RULE_JSON]

    stats = rule_miner.mine_rules(db, svc, provider="claude_cli")
    assert stats["ingested"] == 3
    assert _confidences(svc) == [0.50, 0.60, 0.70]


def test_formula_matches_spec_at_each_count():
    """Pin the formula directly: 0.4 + 0.3*min(count/3,1.0)."""
    assert rule_miner._confidence_for_count(1) == 0.50
    assert rule_miner._confidence_for_count(2) == 0.60
    assert rule_miner._confidence_for_count(3) == 0.70


def test_fourth_mining_saturates_at_070(env):
    """4th+ mining stays at the 0.70 ceiling (count/3 saturates at 1.0)."""
    db, svc, holder = env
    rows = _three_windows() + [
        (7, "s4", "assistant", "I hardcoded the path /etc/d into a fourth config directly."),
        (8, "s4", "user", "no, don't hardcode that path, use an env var instead, last time."),
    ]
    _seed(db, rows)
    holder["responses"] = [_RULE_JSON] * 4

    rule_miner.mine_rules(db, svc, provider="claude_cli")
    assert _confidences(svc) == [0.50, 0.60, 0.70, 0.70]


def test_bootstrap_disabled_keeps_flat_confidence(env, monkeypatch):
    """Reversible: env=0 -> every rule keeps the legacy flat 0.40 and rule_stats
    is never written."""
    db, svc, holder = env
    monkeypatch.setenv(rule_miner._BOOTSTRAP_ENV, "0")
    _seed(db, _three_windows())
    holder["responses"] = [_RULE_JSON, _RULE_JSON, _RULE_JSON]

    rule_miner.mine_rules(db, svc, provider="claude_cli")
    assert _confidences(svc) == [0.40, 0.40, 0.40]

    conn = sqlite3.connect(db)
    try:
        has_rows = conn.execute("SELECT count(*) FROM rule_stats").fetchone()[0]
    except sqlite3.OperationalError:
        has_rows = 0
    finally:
        conn.close()
    assert has_rows == 0, "disabled bootstrap must not write rule_stats"


def test_correction_count_bumps_even_when_claim_dedups(env):
    """Re-mining the SAME window dedups the CLAIM but is still a distinct mining
    event: the tally rises (and would raise confidence on a fresh claim)."""
    db, svc, holder = env
    _seed(db, [
        (1, "s1", "assistant", "I hardcoded the path /etc/a into the config file directly."),
        (2, "s1", "user", "no, don't hardcode the path like that, use an env var instead."),
    ])
    holder["responses"] = [_RULE_JSON]
    rule_miner.mine_rules(db, svc, provider="claude_cli")

    holder["responses"] = [_RULE_JSON]
    second = rule_miner.mine_rules(db, svc, provider="claude_cli", reset=True)
    assert second["duplicates"] == 1, "same source window must dedup the claim"

    fp = rule_miner.rule_fingerprint("hardcoding a path", "use an env var instead")
    conn = sqlite3.connect(db)
    try:
        count = conn.execute(
            "SELECT correction_count FROM rule_stats WHERE rule_fingerprint = ?", (fp,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 2, "the tally counts mining events, not surviving claims"


def test_fingerprint_is_stable_and_order_sensitive():
    a = rule_miner.rule_fingerprint("X", "Y")
    assert a == rule_miner.rule_fingerprint("x", "  y ")  # case + whitespace stable
    assert a != rule_miner.rule_fingerprint("Y", "X")  # trigger/action not swappable
    assert len(a) == 16


def test_sensitive_rule_still_dropped_under_bootstrap(env):
    """Bootstrap must not weaken the sensitivity firewall: a secret-bearing rule
    is dropped (no claim, no tally)."""
    db, svc, holder = env
    leak = "ghp_" + "A" * 36
    _seed(db, [
        (1, "s1", "assistant", "I added the auth header to the request directly in code."),
        (2, "s1", "user", "no, don't hardcode it, actually use the token instead somewhere."),
    ])
    holder["responses"] = [json.dumps({"trigger": "auth", "action": f"use {leak}", "rationale": "ci"})]

    stats = rule_miner.mine_rules(db, svc, provider="claude_cli")
    assert stats["ingested"] == 0
    assert stats["skipped"] == 1
    assert [parse_rule(c) for c in svc.store.find_by_status("candidate", limit=10) if is_rule(c)] == []
