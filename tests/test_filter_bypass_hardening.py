"""Sensitivity-filter hardening for the two raw `INSERT INTO claims` sites that
bypass `svc.ingest`.

WHY this matters (sensitivity-filter invariant, .claude/rules/sensitivity-filter.md):
the filter MUST run on EVERY ingest path. Two raw-INSERT sites historically wrote
claim text straight to the `claims` table without the canonical filter:

  SITE 1 — dream_bridge.dream_ingest() flag-off direct path: raw-INSERTs the
           markdown-parsed note text.
  SITE 2 — llm_steward.run_steward() cycle: raw-INSERTs the source claim's
           text[:200] as a NEW status='confirmed' claim every cycle.

A credential reaching either site is a security incident — the raw secret would be
persisted verbatim and later surfaced by recall. These tests anchor on the
REQUIREMENT (default-deny: a sensitive payload never reaches the claims table) and
on the REGRESSION (a normal, non-sensitive payload still ingests/inserts as before),
NOT on which internal layer enforces it.

Both guards reuse the SAME canonical filter `svc.ingest` uses —
`memorymaster.core.security.sanitize_claim_input` — not a new bespoke filter.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

from memorymaster.bridges.dream_bridge import dream_ingest
from memorymaster.govern import llm_steward
from memorymaster.govern.llm_steward import ExtractionResult, run_steward
from memorymaster.core.service import MemoryService


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "memory.db"
    service = MemoryService(db_path, workspace_root=tmp_path)
    service.init_db()
    return db_path


def _all_claim_text(db_path: Path) -> str:
    """Concatenate every persisted claim field so a leaked secret in ANY column
    (text/subject/predicate/object_value) is detectable by a substring check."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT text, subject, predicate, object_value FROM claims"
        ).fetchall()
    finally:
        conn.close()
    return "\n".join(str(c) for row in rows for c in row if c is not None)


def _claim_count(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT count(*) FROM claims").fetchone()[0]
    finally:
        conn.close()


def _memory_file(memory_dir: Path, filename: str, body: str) -> None:
    (memory_dir / filename).write_text(
        "\n".join(
            [
                "---",
                f'name: "{Path(filename).stem}"',
                'description: "Imported from test memory"',
                'type: "project"',
                "---",
                "",
                body,
                "",
            ]
        ),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# SITE 1 — dream_bridge.dream_ingest()
# --------------------------------------------------------------------------- #
# The literal secret never appears in the source so a passing test cannot leak it
# AND so the assertion is a true substring check, not a self-fulfilling match.
_FAKE_DREAM_SECRET = "sk-ant-" + ("a1b2c3d4" * 4)


def test_dream_ingest_skips_credential_note_no_claim_created(tmp_path: Path) -> None:
    """REQUIREMENT (default-deny): a dream note carrying a credential must create
    NO claim, and the raw secret must never reach the claims table."""
    db_path = _init_db(tmp_path)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _memory_file(
        memory_dir,
        "leaky.md",
        f"Prod deploy used the Anthropic key {_FAKE_DREAM_SECRET} for auth.",
    )

    with patch.dict(os.environ, {"CLAUDE_MEMORY_DIR": str(memory_dir)}):
        stats = dream_ingest(str(db_path))

    assert stats["ingested"] == 0, "sensitive note must not be ingested"
    assert _claim_count(db_path) == 0, "no claim row may be created for a secret"
    assert _FAKE_DREAM_SECRET not in _all_claim_text(db_path), (
        "raw credential must never reach the claims table"
    )


def test_dream_ingest_skips_encoded_credential_note(tmp_path: Path) -> None:
    """REQUIREMENT (default-deny, encoded secrets): a base64-wrapped credential
    survives the parse-layer literal-redaction check (`_redact_text` does not
    decode), so the dream_ingest INSERT guard — which runs the SAME canonical
    `sanitize_claim_input` as svc.ingest (it scans decoded variants) — is the
    load-bearing layer. No claim may be created and the blob must not persist."""
    import base64

    raw = "OPENAI_API_KEY=sk-" + ("a1b2c3d4" * 5)
    blob = base64.b64encode(raw.encode()).decode()

    db_path = _init_db(tmp_path)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _memory_file(memory_dir, "encoded.md", f"Saved deploy config blob: {blob}")

    with patch.dict(os.environ, {"CLAUDE_MEMORY_DIR": str(memory_dir)}):
        stats = dream_ingest(str(db_path))

    assert stats["ingested"] == 0, "encoded-secret note must not be ingested"
    assert _claim_count(db_path) == 0, "no claim row may be created"
    assert blob not in _all_claim_text(db_path), (
        "the encoded credential blob must never reach the claims table"
    )


def test_dream_ingest_normal_note_still_ingests(tmp_path: Path) -> None:
    """REGRESSION: a benign dream note still ingests exactly as before — the guard
    must not over-reject ordinary content."""
    db_path = _init_db(tmp_path)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _memory_file(
        memory_dir,
        "normal.md",
        "The retrieval fusion ranker weights recency above raw cosine score.",
    )

    with patch.dict(os.environ, {"CLAUDE_MEMORY_DIR": str(memory_dir)}):
        stats = dream_ingest(str(db_path))

    assert stats["ingested"] == 1, "a benign note must still ingest"
    assert _claim_count(db_path) == 1
    assert "fusion ranker" in _all_claim_text(db_path)


# --------------------------------------------------------------------------- #
# SITE 2 — llm_steward.run_steward() cycle insert
# --------------------------------------------------------------------------- #
# The cycle inserts the SOURCE candidate's text[:200] as a NEW confirmed claim
# when the LLM returns >=2 non-archive extractions (extras[1:] are inserted). A
# candidate is seeded with raw SQL (bypassing svc.ingest, exactly the real-world
# shape where an un-sanitised candidate exists) so its text[:200] would be the
# secret about to be re-persisted.
_FAKE_STEWARD_SECRET = "sk-" + ("deadbeef" * 5)  # openai_key-shaped, 40+ body


def _insert_candidate(db_path: Path, text: str, scope: str = "project:test") -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO claims (text, status, scope, confidence, created_at, updated_at) "
            "VALUES (?, 'candidate', ?, 0.5, datetime('now'), datetime('now'))",
            (text, scope),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


_TWO_EXTRACTIONS = [
    {"subject": "s-first", "predicate": "p-first", "object_value": "o1", "confidence": 0.8},
    {"subject": "s-extra-unique", "predicate": "p-extra-unique", "object_value": "o2", "confidence": 0.8},
]


def _run(db_path: Path, monkeypatch) -> dict:
    def fake_extract(provider, api_key, model, claim_id, text,
                     base_url="", key_rotator=None, use_llm_provider=False):
        # >=2 non-archive extractions -> first confirms the source, extras[1:]
        # insert a NEW claim whose text is the SOURCE candidate's text[:200].
        return ExtractionResult(
            claim_id=claim_id,
            original_text=text,
            extractions=list(_TWO_EXTRACTIONS),
            action="confirm",
            raw_response="[]",
        )

    monkeypatch.setattr(llm_steward, "extract_claim", fake_extract)
    return run_steward(
        str(db_path), api_key="x", provider="gemini", limit=100, delay=0.0,
        auto_validate=False,
    )


def test_steward_cycle_skips_sensitive_extraction(tmp_path: Path, monkeypatch) -> None:
    """REQUIREMENT (default-deny): when the source candidate's text carries a
    credential, the steward cycle insert must SKIP — no new claim is created
    carrying the secret, and the counter records the skip."""
    monkeypatch.delenv("QDRANT_URL", raising=False)
    db_path = _init_db(tmp_path)
    _insert_candidate(
        db_path, f"Found the prod API key {_FAKE_STEWARD_SECRET} in the deploy log."
    )

    before = _claim_count(db_path)
    stats = _run(db_path, monkeypatch)

    assert stats["claims_extracted"] == 0, "no new claim may be extracted from a secret"
    assert stats["claims_filtered_sensitive"] >= 1, "the skip must be counted"
    # The raw-INSERT bypass site must create NO new row. (The source candidate is
    # confirmed in place via a separate path — that pre-existing row already held
    # the secret and is not what this site inserts; the invariant under test is
    # that the cycle does not MINT a brand-new claim carrying the secret text.)
    assert _claim_count(db_path) == before, "no new claim row may be inserted"
    # The extra-extraction row (subject 's-extra-unique') — the one this site would
    # have inserted — must NOT exist, and no such inserted text carries the secret.
    assert not _rows_with_subject(db_path, "s-extra-unique"), (
        "the steward cycle insert must be skipped for a sensitive source"
    )


def test_steward_cycle_normal_extraction_still_inserts(tmp_path: Path, monkeypatch) -> None:
    """REGRESSION: a benign source candidate still yields the extra-extraction
    insert — the guard must not block ordinary cycle inserts."""
    monkeypatch.delenv("QDRANT_URL", raising=False)
    db_path = _init_db(tmp_path)
    _insert_candidate(
        db_path, "The compactor merges summaries older than the freshness window."
    )

    stats = _run(db_path, monkeypatch)

    assert stats["claims_filtered_sensitive"] == 0, "benign text must not be filtered"
    assert stats["claims_extracted"] == 1, "a benign extra extraction must still insert"
    # The raw-INSERT bypass site actually minted the extra-extraction row.
    extra_rows = _rows_with_subject(db_path, "s-extra-unique")
    assert len(extra_rows) == 1, "the benign extra extraction must be inserted"
    assert "compactor merges summaries" in extra_rows[0][0], (
        "the inserted row carries the source candidate's text[:200]"
    )


def _rows_with_subject(db_path: Path, subject: str) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT text, subject, predicate, object_value FROM claims WHERE subject = ?",
            (subject,),
        ).fetchall()
    finally:
        conn.close()
