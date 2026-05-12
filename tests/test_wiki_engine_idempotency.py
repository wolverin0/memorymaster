from __future__ import annotations

import sqlite3
from pathlib import Path

from memorymaster.storage import SQLiteStore
from memorymaster.wiki_engine import absorb


class _ChangingDateTime:
    calls = 0

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001
        from datetime import datetime

        cls.calls += 1
        if cls.calls <= 3:
            return datetime(2026, 1, 10, 9, 0, 0, tzinfo=tz)
        return datetime(2026, 2, 20, 9, 0, 0, tzinfo=tz)


def _fresh_db(tmp_path: Path) -> Path:
    db = tmp_path / "memory.db"
    store = SQLiteStore(str(db))
    store.init_db()
    return db


def _insert_claim(
    conn: sqlite3.Connection,
    *,
    text: str,
    claim_type: str,
    predicate: str,
    object_value: str,
    confidence: float,
    human_id: str,
    updated_at: str,
    citations: list[tuple[str, str, str]],
) -> int:
    cur = conn.execute(
        """INSERT INTO claims (text, claim_type, subject, predicate, object_value,
                               scope, status, confidence, created_at, updated_at,
                               event_time, valid_from, human_id, tier, version)
           VALUES (?, ?, 'wiki absorb', ?, ?, 'project:test', 'candidate', ?,
                   '2026-01-01T00:00:00+00:00', ?, ?, '2026-01-01',
                   ?, 'working', 1)""",
        (
            text,
            claim_type,
            predicate,
            object_value,
            confidence,
            updated_at,
            updated_at,
            human_id,
        ),
    )
    claim_id = int(cur.lastrowid)
    for source, locator, excerpt in citations:
        conn.execute(
            """INSERT INTO citations (claim_id, source, locator, excerpt, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (claim_id, source, locator, excerpt, updated_at),
        )
    return claim_id


def _seed_claims(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    try:
        _insert_claim(
            conn,
            text="wiki absorb groups active claims by subject",
            claim_type="fact",
            predicate="groups",
            object_value="by subject",
            confidence=0.93,
            human_id="mm-test-1",
            updated_at="2026-01-05T00:00:00+00:00",
            citations=[("docs", "wiki.md:10", "groups active claims")],
        )
        _insert_claim(
            conn,
            text="wiki absorb writes a compiled truth section",
            claim_type="decision",
            predicate="writes",
            object_value="compiled truth",
            confidence=0.88,
            human_id="mm-test-2",
            updated_at="2026-01-07T00:00:00+00:00",
            citations=[("notes", "session-2", "compiled truth")],
        )
        _insert_claim(
            conn,
            text="wiki absorb appends timeline evidence below a separator",
            claim_type="constraint",
            predicate="appends",
            object_value="timeline",
            confidence=0.84,
            human_id="mm-test-3",
            updated_at="2026-01-09T00:00:00+00:00",
            citations=[("notes", "session-3", "timeline")],
        )
        _insert_claim(
            conn,
            text="wiki absorb should preserve deterministic file formatting",
            claim_type="fact",
            predicate="preserves",
            object_value="formatting",
            confidence=0.79,
            human_id="mm-test-4",
            updated_at="2026-01-03T00:00:00+00:00",
            citations=[
                ("ticket", "T13", "formatting"),
                ("docs", "wiki.md:20", "deterministic formatting"),
            ],
        )
        _insert_claim(
            conn,
            text="wiki absorb output is compared byte by byte",
            claim_type="fact",
            predicate="compares",
            object_value="bytes",
            confidence=0.75,
            human_id="mm-test-5",
            updated_at="2026-01-04T00:00:00+00:00",
            citations=[("ticket", "T13", "byte comparison")],
        )
        conn.commit()
    finally:
        conn.close()


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_wiki_absorb_compiled_truth_output_is_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = _fresh_db(tmp_path)
    _seed_claims(db)

    def fake_llm(prompt: str, text: str) -> str:  # noqa: ARG001
        return (
            "Wiki absorb compiles claims for [[wiki absorb]] into a stable article.\n\n"
            "## Compiled Truth\n"
            "The absorb process groups active claims and writes deterministic markdown.\n\n"
            "---\n\n"
            "## Timeline\n"
            "### 2026-01-09 | test\n"
            "Synthetic evidence entry."
        )

    monkeypatch.setattr("memorymaster.wiki_engine._call_llm", fake_llm)
    monkeypatch.setattr("memorymaster.wiki_engine.datetime", _ChangingDateTime)

    first = tmp_path / "wiki-first"
    second = tmp_path / "wiki-second"
    absorb(str(db), first, scope_filter="project:test")
    absorb(str(db), second, scope_filter="project:test")

    assert _snapshot(first) == _snapshot(second)
