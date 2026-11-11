"""Unit tests for v3.13 pre-steward candidate dedupe."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memorymaster.govern.candidate_dedupe import (
    DedupeResult,
    jaccard_high_threshold,
    fts_candidates_in_scope,
    find_near_duplicate,
    is_enabled,
    is_shadow_mode,
)

_SCHEMA = """
CREATE TABLE claims (
    id INTEGER PRIMARY KEY,
    text TEXT,
    scope TEXT,
    status TEXT,
    replaced_by_claim_id INTEGER,
    access_count INTEGER DEFAULT 0,
    updated_at TEXT,
    supersedes_claim_id INTEGER,
    tenant_id TEXT,
    visibility TEXT DEFAULT 'public'
);
CREATE TABLE claim_links (source_id INTEGER, target_id INTEGER, link_type TEXT);
CREATE TABLE events (
    id INTEGER PRIMARY KEY, claim_id INTEGER, event_type TEXT, details TEXT, payload_json TEXT
);
CREATE VIRTUAL TABLE claims_fts USING fts5(text, content='claims', content_rowid='id');
CREATE TRIGGER claims_ai AFTER INSERT ON claims BEGIN
    INSERT INTO claims_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER claims_ad AFTER DELETE ON claims BEGIN
    INSERT INTO claims_fts(claims_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER claims_au AFTER UPDATE ON claims BEGIN
    INSERT INTO claims_fts(claims_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO claims_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(_SCHEMA)
    c.commit()
    return c


def _ins(
    conn: sqlite3.Connection,
    *,
    text: str,
    scope: str = "project:memorymaster",
    status: str = "confirmed",
) -> int:
    cur = conn.execute(
        "INSERT INTO claims (text, scope, status) VALUES (?, ?, ?)",
        (text, scope, status),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_env_flag_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMORYMASTER_DEDUPE_ENABLED", raising=False)
    monkeypatch.delenv("MEMORYMASTER_DEDUPE_SHADOW", raising=False)
    monkeypatch.delenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", raising=False)
    assert is_enabled() is False
    assert is_shadow_mode() is True
    assert jaccard_high_threshold() == pytest.approx(0.85)


def test_env_flag_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_ENABLED", "true")
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_SHADOW", "0")
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.5")
    assert is_enabled() is True
    assert is_shadow_mode() is False
    assert jaccard_high_threshold() == pytest.approx(0.5)


def test_env_flag_invalid_threshold_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "not-a-number")
    assert jaccard_high_threshold() == pytest.approx(0.85)


def test_too_short_text_passthrough(conn: sqlite3.Connection) -> None:
    cid = _ins(conn, text="hi")
    result = find_near_duplicate(
        conn,
        candidate_id=cid,
        candidate_text="hi",
        candidate_scope="project:memorymaster",
    )
    assert result.action == "passthrough"
    assert result.reason == "text-too-short"


def test_no_existing_claims_passthrough(conn: sqlite3.Connection) -> None:
    cid = _ins(
        conn,
        text="user prefers vim editor with vim plugins for python",
        status="candidate",
    )
    result = find_near_duplicate(
        conn,
        candidate_id=cid,
        candidate_text="user prefers vim editor with vim plugins for python",
        candidate_scope="project:memorymaster",
    )
    assert result.action == "passthrough"
    assert result.reason == "no-fts-matches"


def test_paraphrase_detected_as_archive(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.5")
    canonical = _ins(
        conn,
        text="user prefers vim editor with vim plugins for python development",
        status="confirmed",
    )
    candidate = _ins(
        conn,
        text="user prefers vim editor with vim plugins for python development tasks",
        status="candidate",
    )
    result = find_near_duplicate(
        conn,
        candidate_id=candidate,
        candidate_text="user prefers vim editor with vim plugins for python development tasks",
        candidate_scope="project:memorymaster",
    )
    assert result.action == "archive"
    assert result.canonical_claim_id == canonical
    assert result.jaccard_score is not None
    assert result.jaccard_score >= 0.5


def test_different_subject_passthrough(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.85")
    _ins(
        conn,
        text="alice runs the linux kernel patches in production for storage",
        status="confirmed",
    )
    candidate = _ins(
        conn,
        text="bob enjoys windows powershell automation in his daily workflow",
        status="candidate",
    )
    result = find_near_duplicate(
        conn,
        candidate_id=candidate,
        candidate_text="bob enjoys windows powershell automation in his daily workflow",
        candidate_scope="project:memorymaster",
    )
    assert result.action == "passthrough"


def test_cross_scope_isolation(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.1")
    _ins(
        conn,
        text="user prefers vim editor with vim plugins for python development",
        scope="project:other",
        status="confirmed",
    )
    candidate = _ins(
        conn,
        text="user prefers vim editor with vim plugins for python development",
        scope="project:memorymaster",
        status="candidate",
    )
    result = find_near_duplicate(
        conn,
        candidate_id=candidate,
        candidate_text="user prefers vim editor with vim plugins for python development",
        candidate_scope="project:memorymaster",
    )
    assert result.action == "passthrough"
    assert result.reason == "no-fts-matches"


def test_archived_claims_excluded(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.1")
    _ins(
        conn,
        text="user prefers vim editor with vim plugins for python development",
        status="archived",
    )
    candidate = _ins(
        conn,
        text="user prefers vim editor with vim plugins for python development",
        status="candidate",
    )
    result = find_near_duplicate(
        conn,
        candidate_id=candidate,
        candidate_text="user prefers vim editor with vim plugins for python development",
        candidate_scope="project:memorymaster",
    )
    assert result.action == "passthrough"


def test_threshold_too_high_passthrough(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.95")
    _ins(
        conn,
        text="user prefers vim editor with plugins for python development",
        status="confirmed",
    )
    candidate = _ins(
        conn,
        text="user prefers emacs editor with plugins for ruby scripting",
        status="candidate",
    )
    result = find_near_duplicate(
        conn,
        candidate_id=candidate,
        candidate_text="user prefers emacs editor with plugins for ruby scripting",
        candidate_scope="project:memorymaster",
    )
    assert result.action == "passthrough"
    assert result.jaccard_score is not None
    assert result.jaccard_score < 0.95


def test_candidate_vs_candidate_can_match(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.1")
    older = _ins(
        conn,
        text="user prefers vim editor with vim plugins for python development",
        status="candidate",
    )
    newer = _ins(
        conn,
        text="user prefers vim editor with vim plugins for python development",
        status="candidate",
    )
    result = find_near_duplicate(
        conn,
        candidate_id=newer,
        candidate_text="user prefers vim editor with vim plugins for python development",
        candidate_scope="project:memorymaster",
    )
    assert result.action == "archive"
    assert result.canonical_claim_id == older


def test_fts_candidates_returns_matches(conn: sqlite3.Connection) -> None:
    _ins(
        conn,
        text="memorymaster steward processes candidates via llm extraction",
    )
    candidate = _ins(
        conn,
        text="memorymaster steward processes candidates via llm extraction",
        status="candidate",
    )
    rows = fts_candidates_in_scope(
        conn,
        scope="project:memorymaster",
        text="memorymaster steward processes candidates via llm extraction",
        exclude_id=candidate,
        limit=5,
    )
    assert rows
    assert len(rows[0]) == 3


def test_jaccard_known_values() -> None:
    from memorymaster.govern.candidate_dedupe import jaccard_tokens
    assert jaccard_tokens("the cat sat", "the cat sat") == pytest.approx(1.0)
    assert jaccard_tokens("the cat sat", "the dog sat") == pytest.approx(2 / 4)
    assert jaccard_tokens("alpha beta gamma", "delta epsilon zeta") == 0.0
    assert jaccard_tokens("", "") == 0.0


def test_dedupe_result_is_immutable() -> None:
    r = DedupeResult(action="passthrough", canonical_claim_id=None,
                     jaccard_score=None, reason="test")
    with pytest.raises(Exception):
        r.action = "archive"  # type: ignore[misc]


# --- X-1 / G-1: a correction is not a duplicate, and an archive goes through the lifecycle path -----------------

_PORT = (
    "The staging admin panel for the zorblax billing service listens on port {} "
    "behind the internal reverse proxy since the October migration"
)
_PROPOSAL = "steward_proposal:superseded_candidate"
_LONG = "Alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike"


def test_value_token_difference_blocks_archive(conn: sqlite3.Connection) -> None:
    _ins(conn, text=_PORT.format(8010), status="confirmed")
    cand = _ins(conn, text=_PORT.format(9413), status="candidate")
    result = find_near_duplicate(
        conn, candidate_id=cand, candidate_text=_PORT.format(9413), candidate_scope="project:memorymaster"
    )
    assert result.jaccard_score is not None and result.jaccard_score >= 0.85
    assert (result.action, result.reason) == ("passthrough", "value-tokens-differ")


def test_single_digit_value_difference_blocks_archive(conn: sqlite3.Connection) -> None:
    text = "The retry budget for the zorblax exporter job during the nightly batch window is {} attempts per run"
    _ins(conn, text=text.format(3), status="confirmed")
    cand = _ins(conn, text=text.format(5), status="candidate")
    result = find_near_duplicate(
        conn, candidate_id=cand, candidate_text=text.format(5), candidate_scope="project:memorymaster"
    )
    assert result.reason == "value-tokens-differ"


@pytest.mark.parametrize("guard", ["supersedes_claim_id", "link", "proposal", "rejected-proposal"])
def test_correction_markers_block_archive(conn: sqlite3.Connection, guard: str) -> None:
    canon = _ins(conn, text=_LONG, status="confirmed")
    cand = _ins(conn, text=_LONG + " november", status="candidate")
    if guard == "supersedes_claim_id":
        conn.execute("UPDATE claims SET supersedes_claim_id = ? WHERE id = ?", (canon, cand))
    elif guard == "link":
        conn.execute("INSERT INTO claim_links VALUES (?, ?, 'supersedes')", (cand, canon))
    else:
        conn.execute(
            "INSERT INTO events (id, claim_id, event_type, details, payload_json) "
            "VALUES (100, ?, 'policy_decision', ?, ?)",
            (canon, _PROPOSAL, json.dumps({"replaced_by_claim_id": cand})),
        )
        if guard == "rejected-proposal":
            conn.execute(
                "INSERT INTO events (claim_id, event_type, details, payload_json) "
                "VALUES (?, 'audit', 'steward_proposal_rejected', ?)",
                (canon, json.dumps({"proposal_event_id": 100})),
            )
    conn.commit()
    result = find_near_duplicate(
        conn, candidate_id=cand, candidate_text=_LONG + " november", candidate_scope="project:memorymaster"
    )
    if guard == "rejected-proposal":
        assert result.action == "archive"  # a rejected proposal no longer marks a correction
    else:
        assert result.action == "passthrough" and result.reason.startswith("correction-guard:")


def test_tenant_or_visibility_mismatch_blocks_archive(conn: sqlite3.Connection) -> None:
    _ins(conn, text=_LONG, status="confirmed")
    cand = _ins(conn, text=_LONG + " november", status="candidate")
    conn.execute("UPDATE claims SET visibility = 'private' WHERE id = ?", (cand,))
    conn.commit()
    result = find_near_duplicate(
        conn, candidate_id=cand, candidate_text=_LONG + " november", candidate_scope="project:memorymaster"
    )
    assert result.reason == "correction-guard:tenant-or-visibility-differs"


# End to end through MemoryService.run_cycle + curation drain, with the LIVE settings (enabled, shadow off, 0.85).


@pytest.fixture
def live_dedupe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from memorymaster.core import llm_provider
    from memorymaster.core.service import MemoryService

    monkeypatch.setenv("MEMORYMASTER_DEDUPE_ENABLED", "1")
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_SHADOW", "0")
    monkeypatch.setenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", "0.85")

    def _no(*a, **k):
        raise RuntimeError("provider stubbed")

    monkeypatch.setattr(llm_provider, "_http_post", _no)
    svc = MemoryService(tmp_path / "x1.db", workspace_root=tmp_path)
    svc.init_db()
    return svc


def _ingest(svc, text: str, agent: str, *, confirm: bool = False, **kw):
    from memorymaster.core import lifecycle
    from memorymaster.core.models import CitationInput

    claim = svc.ingest(
        text, [CitationInput(source="test://x1", locator="x")], scope="project:x1", source_agent=agent, **kw
    )
    if confirm:
        claim = lifecycle.transition_claim(svc.store, claim.id, "confirmed", reason="fixture", event_type="validator")
    return claim


def _cycle_and_drain(svc) -> dict:
    from memorymaster.govern.jobs import curation_drain

    res = svc.run_cycle(batch_limit=200)
    curation_drain.run(svc, limit=200, apply=True)
    return res


def test_declared_one_value_correction_survives_cycle_and_drain(live_dedupe) -> None:
    svc = live_dedupe
    a = _ingest(svc, _PORT.format(8010), "codex", confirm=True)
    b = _ingest(svc, _PORT.format(9413), "codex", supersedes=[a.id])
    res = _cycle_and_drain(svc)
    a2 = svc.store.get_claim(a.id, include_citations=False)
    b2 = svc.store.get_claim(b.id, include_citations=False)
    assert res["dedupe"]["archived"] == 0
    assert b2.status != "archived", (b2.status, b2.replaced_by_claim_id)
    assert a2.status == "superseded" and a2.replaced_by_claim_id == b.id


def test_undeclared_one_value_correction_is_not_archived(live_dedupe) -> None:
    svc = live_dedupe
    _ingest(svc, _PORT.format(8010), "codex", confirm=True)
    b = _ingest(svc, _PORT.format(9413), "codex")
    res = _cycle_and_drain(svc)
    assert res["dedupe"]["archived"] == 0
    assert svc.store.get_claim(b.id, include_citations=False).status != "archived"


def test_true_duplicate_is_archived_through_lifecycle_path(live_dedupe) -> None:
    svc = live_dedupe
    a = _ingest(svc, _PORT.format(8010), "codex", confirm=True)
    b = _ingest(svc, _PORT.format(8010) + " today", "bob")
    before = svc.store.get_claim(b.id, include_citations=False)
    res = svc.run_cycle(batch_limit=200)
    after = svc.store.get_claim(b.id, include_citations=False)
    assert res["dedupe"]["archived"] == 1
    assert after.status == "archived" and after.replaced_by_claim_id == a.id
    assert after.version == before.version + 1
    assert after.archived_at is not None
    with svc.store.connect() as conn:
        ev = conn.execute(
            "SELECT from_status, to_status, event_hash FROM events "
            "WHERE claim_id = ? AND details LIKE 'dedupe-archived%'",
            (b.id,),
        ).fetchall()
    assert len(ev) == 1 and ev[0][0] == "candidate" and ev[0][1] == "archived" and ev[0][2]


def test_tenant_visibility_mismatch_not_archived_end_to_end(live_dedupe) -> None:
    svc = live_dedupe
    _ingest(svc, _PORT.format(8010), "codex", confirm=True)
    b = _ingest(svc, _PORT.format(8010) + " today", "bob", visibility="private", holder="bob")
    res = svc.run_cycle(batch_limit=200)
    assert res["dedupe"]["archived"] == 0
    assert svc.store.get_claim(b.id, include_citations=False).status != "archived"
