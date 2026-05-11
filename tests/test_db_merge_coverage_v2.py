import sqlite3
from pathlib import Path

from memorymaster.db_merge import merge_databases


BASE_TIME = "2026-05-11T12:00:00+00:00"


def _init_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                idempotency_key TEXT UNIQUE,
                claim_type TEXT,
                subject TEXT,
                predicate TEXT,
                object_value TEXT,
                scope TEXT NOT NULL DEFAULT 'project:test',
                status TEXT NOT NULL DEFAULT 'candidate',
                confidence REAL NOT NULL DEFAULT 0.5,
                pinned INTEGER NOT NULL DEFAULT 0,
                supersedes_claim_id INTEGER,
                replaced_by_claim_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived_at TEXT,
                source_agent TEXT
            );

            CREATE TABLE citations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                locator TEXT,
                excerpt TEXT,
                created_at TEXT NOT NULL
            );
            """
        )


def _insert_claim(
    path: Path,
    *,
    text: str,
    idempotency_key: str | None = None,
    subject: str = "sync",
    predicate: str = "state",
    object_value: str = "ok",
    scope: str = "project:test",
    status: str = "candidate",
    confidence: float = 0.5,
    pinned: bool = False,
    updated_at: str = BASE_TIME,
) -> int:
    with sqlite3.connect(path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO claims (
                text, idempotency_key, claim_type, subject, predicate, object_value,
                scope, status, confidence, pinned, created_at, updated_at, source_agent
            )
            VALUES (?, ?, 'fact', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'test')
            """,
            (
                text,
                idempotency_key,
                subject,
                predicate,
                object_value,
                scope,
                status,
                confidence,
                1 if pinned else 0,
                BASE_TIME,
                updated_at,
            ),
        )
        return int(cursor.lastrowid)


def _insert_citation(
    path: Path,
    claim_id: int,
    *,
    source: str = "test-source",
    locator: str = "line 1",
    excerpt: str = "supporting excerpt",
) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO citations (claim_id, source, locator, excerpt, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (claim_id, source, locator, excerpt, BASE_TIME),
        )


def _claims(path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM claims ORDER BY id").fetchall()
    finally:
        conn.close()


def _citations(path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM citations ORDER BY id").fetchall()
    finally:
        conn.close()


def _snapshot(path: Path) -> tuple[list[tuple], list[tuple]]:
    claim_rows = [
        tuple(row[key] for key in row.keys() if key != "id")
        for row in _claims(path)
    ]
    citation_rows = [
        tuple(row[key] for key in row.keys() if key != "id")
        for row in _citations(path)
    ]
    return claim_rows, citation_rows


def test_empty_source_db_scans_nothing_and_leaves_target_unchanged(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _init_db(source)
    _init_db(target)
    _insert_claim(target, text="Existing local claim", idempotency_key="local-1")

    before = _snapshot(target)
    stats = merge_databases(str(target), str(source))

    assert stats == {"scanned": 0, "merged": 0, "skipped": 0, "errors": 0}
    assert _snapshot(target) == before


def test_empty_target_db_receives_all_source_claims(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _init_db(source)
    _init_db(target)
    _insert_claim(source, text="Remote claim A", idempotency_key="remote-a")
    _insert_claim(source, text="Remote claim B", idempotency_key="remote-b")

    stats = merge_databases(str(target), str(source))

    assert stats == {"scanned": 2, "merged": 2, "skipped": 0, "errors": 0}
    assert [row["text"] for row in _claims(target)] == ["Remote claim A", "Remote claim B"]


def test_same_tuple_different_object_uses_winner_priority_logic(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _init_db(source)
    _init_db(target)
    _insert_claim(
        target,
        text="Feature flag is disabled",
        idempotency_key="local-loser",
        object_value="disabled",
        confidence=0.4,
    )
    _insert_claim(
        source,
        text="Feature flag is enabled",
        idempotency_key="remote-winner",
        object_value="enabled",
        confidence=0.9,
    )

    stats = merge_databases(str(target), str(source))
    rows = _claims(target)
    by_key = {row["idempotency_key"]: row for row in rows}

    assert stats == {"scanned": 1, "merged": 1, "skipped": 0, "errors": 0}
    assert by_key["remote-winner"]["status"] == "candidate"
    assert by_key["local-loser"]["status"] == "superseded"
    assert by_key["local-loser"]["replaced_by_claim_id"] == by_key["remote-winner"]["id"]


def test_pinned_claim_takes_merge_precedence_over_higher_confidence_source(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _init_db(source)
    _init_db(target)
    pinned_id = _insert_claim(
        target,
        text="Deployment owner is Alice",
        idempotency_key="local-pinned",
        subject="deployment",
        predicate="owner",
        object_value="Alice",
        confidence=0.2,
        pinned=True,
    )
    _insert_claim(
        source,
        text="Deployment owner is Bob",
        idempotency_key="remote-loser",
        subject="deployment",
        predicate="owner",
        object_value="Bob",
        confidence=0.99,
    )

    stats = merge_databases(str(target), str(source))
    by_key = {row["idempotency_key"]: row for row in _claims(target)}

    assert stats == {"scanned": 1, "merged": 1, "skipped": 0, "errors": 0}
    assert by_key["local-pinned"]["id"] == pinned_id
    assert by_key["local-pinned"]["status"] == "candidate"
    assert by_key["remote-loser"]["status"] == "superseded"
    assert by_key["remote-loser"]["replaced_by_claim_id"] == pinned_id


def test_idempotent_remerge_does_not_change_claims_or_citations(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _init_db(source)
    _init_db(target)
    source_id = _insert_claim(source, text="Remote singleton", idempotency_key="remote-single")
    _insert_citation(source, source_id)

    first = merge_databases(str(target), str(source))
    after_first = _snapshot(target)
    second = merge_databases(str(target), str(source))

    assert first == {"scanned": 1, "merged": 1, "skipped": 0, "errors": 0}
    assert second == {"scanned": 1, "merged": 0, "skipped": 1, "errors": 0}
    assert _snapshot(target) == after_first


def test_citations_are_preserved_across_merge(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _init_db(source)
    _init_db(target)
    source_id = _insert_claim(source, text="Claim with citations", idempotency_key="cited")
    _insert_citation(source, source_id, source="doc-a", locator="p1", excerpt="first")
    _insert_citation(source, source_id, source="doc-b", locator="p2", excerpt="second")

    stats = merge_databases(str(target), str(source))
    citations = _citations(target)

    assert stats == {"scanned": 1, "merged": 1, "skipped": 0, "errors": 0}
    assert [(row["source"], row["locator"], row["excerpt"]) for row in citations] == [
        ("doc-a", "p1", "first"),
        ("doc-b", "p2", "second"),
    ]
