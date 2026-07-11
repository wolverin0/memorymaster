from __future__ import annotations

import base64
import logging
import sqlite3
from pathlib import Path

import pytest

import memorymaster.bridges.db_merge as db_merge_module
from memorymaster.bridges.db_merge import merge_databases
from memorymaster.bridges.delta_sync import export_delta
from memorymaster.bridges.persisted_envelope import sanitize_claim_envelope
from memorymaster.core.models import CitationInput
from memorymaster.core.security import SensitiveMetadataError
from memorymaster.core.service import MemoryService


LITERAL = "OPENAI_API_KEY=sk-proj-FAKEbridgeEnvelope1234567890ABCD"
ENCODED = base64.b64encode(LITERAL.encode()).decode()


def _service(path: Path, workspace: Path) -> MemoryService:
    service = MemoryService(path, workspace_root=workspace)
    service.init_db()
    return service


def _claim(service: MemoryService, text: str = "Safe bridge claim"):
    return service.ingest(
        text,
        [CitationInput(source="test://bridge", locator="line-1", excerpt="safe")],
        source_agent="bridge-test",
    )


def _update(path: Path, statement: str, values: tuple[object, ...]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(statement, values)


def _column(path: Path, table: str, field: str) -> object:
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT {field} FROM {table}").fetchone()[0]


def _seed_duplicate_claims(path: Path, count: int, *, source: bool) -> None:
    timestamp = "2030-01-01T00:00:00+00:00" if source else "2020-01-01T00:00:00+00:00"
    confidence = 0.9 if source else 0.2
    rows = [
        (f"duplicate-{index}", f"duplicate-key-{index}", confidence, timestamp, timestamp)
        for index in range(count)
    ]
    with sqlite3.connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO claims
                (text, idempotency_key, scope, volatility, status, confidence,
                 pinned, created_at, updated_at, visibility)
            VALUES (?, ?, 'project', 'medium', 'candidate', ?, 0, ?, ?, 'public')
            """,
            rows,
        )


def test_bridge_envelope_rejects_secret_bearing_field_names_without_echo() -> None:
    with pytest.raises(SensitiveMetadataError) as rejected:
        sanitize_claim_envelope({"text": "safe", LITERAL: "safe"})

    assert LITERAL not in str(rejected.value)


def test_merge_rejection_log_never_echoes_an_unsafe_legacy_identifier(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = tmp_path / "legacy-source.db"
    target = tmp_path / "target.db"
    _service(target, tmp_path)
    with sqlite3.connect(source) as conn:
        conn.executescript(
            """
            CREATE TABLE claims (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                idempotency_key TEXT,
                scope TEXT NOT NULL,
                status TEXT NOT NULL,
                confidence REAL NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE citations (
                id INTEGER PRIMARY KEY,
                claim_id TEXT NOT NULL,
                source TEXT NOT NULL,
                locator TEXT,
                excerpt TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO claims
                (id, text, idempotency_key, scope, status, confidence, created_at, updated_at)
            VALUES (?, 'safe', 'safe-key', 'project', 'candidate', 0.5, ?, ?)
            """,
            (LITERAL, "2026-07-11T00:00:00+00:00", "2026-07-11T00:00:00+00:00"),
        )

    with caplog.at_level(logging.WARNING):
        stats = merge_databases(str(target), str(source))

    assert stats["errors"] == 1
    assert LITERAL not in caplog.text


def test_merge_quotes_legacy_column_identifiers(tmp_path: Path) -> None:
    source = tmp_path / "legacy-source.db"
    target = tmp_path / "legacy-target.db"
    schema = """
        CREATE TABLE claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            idempotency_key TEXT,
            scope TEXT NOT NULL,
            status TEXT NOT NULL,
            confidence REAL NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            "select" TEXT
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
    for path in (source, target):
        with sqlite3.connect(path) as conn:
            conn.executescript(schema)
    with sqlite3.connect(source) as conn:
        conn.execute(
            """
            INSERT INTO claims
                (text, idempotency_key, scope, status, confidence,
                 created_at, updated_at, "select")
            VALUES ('safe', 'reserved-key', 'project', 'candidate', 0.5, ?, ?, 'kept')
            """,
            ("2026-07-11T00:00:00+00:00", "2026-07-11T00:00:00+00:00"),
        )

    stats = merge_databases(str(target), str(source))

    assert stats["merged"] == 1
    assert _column(target, "claims", '"select"') == "kept"


@pytest.mark.parametrize("secret", [LITERAL, ENCODED])
def test_merge_rejects_unsafe_claim_metadata_before_target_write(
    tmp_path: Path, secret: str
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    source_service = _service(source, tmp_path)
    _service(target, tmp_path)
    claim = _claim(source_service)
    _update(
        source,
        "UPDATE claims SET idempotency_key = ? WHERE id = ?",
        (secret, claim.id),
    )

    stats = merge_databases(str(target), str(source))

    assert stats == {"scanned": 1, "merged": 0, "skipped": 0, "errors": 1}
    assert _column(target, "claims", "COUNT(*)") == 0
    assert secret.encode() not in target.read_bytes()


def test_merge_sanitizes_claim_and_citation_content_atomically(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    source_service = _service(source, tmp_path)
    _service(target, tmp_path)
    claim = _claim(source_service)
    _update(
        source,
        "UPDATE claims SET text = ?, normalized_text = ? WHERE id = ?",
        (LITERAL, ENCODED, claim.id),
    )
    _update(
        source,
        "UPDATE citations SET excerpt = ? WHERE claim_id = ?",
        (ENCODED, claim.id),
    )

    stats = merge_databases(str(target), str(source))

    assert stats["merged"] == 1
    assert "[REDACTED:" in str(_column(target, "claims", "text"))
    assert "[REDACTED:" in str(_column(target, "claims", "normalized_text"))
    assert "[REDACTED:" in str(_column(target, "citations", "excerpt"))
    persisted = target.read_bytes()
    assert LITERAL.encode() not in persisted
    assert ENCODED.encode() not in persisted


def test_merge_does_not_mutate_an_unsafe_legacy_target_row(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    source_service = _service(source, tmp_path)
    target_service = _service(target, tmp_path)
    source_claim = _claim(source_service, "Shared bridge claim")
    target_claim = _claim(target_service, "Shared bridge claim")
    _update(
        source,
        "UPDATE claims SET confidence = 0.9, updated_at = ? WHERE id = ?",
        ("2030-01-01T00:00:00+00:00", source_claim.id),
    )
    _update(
        target,
        "UPDATE claims SET normalized_text = ?, confidence = 0.2 WHERE id = ?",
        (ENCODED, target_claim.id),
    )

    merge_databases(str(target), str(source))

    assert _column(target, "claims", "confidence") == 0.2


def test_duplicate_with_unsafe_source_citation_cannot_reconcile_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    source_service = _service(source, tmp_path)
    target_service = _service(target, tmp_path)
    source_claim = _claim(source_service, "Shared citation envelope")
    target_claim = _claim(target_service, "Shared citation envelope")
    _update(
        source,
        "UPDATE claims SET confidence = 0.9, updated_at = ? WHERE id = ?",
        ("2030-01-01T00:00:00+00:00", source_claim.id),
    )
    _update(
        source,
        "UPDATE citations SET source = ? WHERE claim_id = ?",
        (LITERAL, source_claim.id),
    )
    _update(
        target,
        "UPDATE claims SET confidence = 0.2 WHERE id = ?",
        (target_claim.id,),
    )

    stats = merge_databases(str(target), str(source))

    assert stats == {"scanned": 1, "merged": 0, "skipped": 0, "errors": 1}
    assert _column(target, "claims", "confidence") == 0.2


def test_target_with_unsafe_citation_cannot_be_reconciled(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    source_service = _service(source, tmp_path)
    target_service = _service(target, tmp_path)
    source_claim = _claim(source_service, "Shared target citation envelope")
    target_claim = _claim(target_service, "Shared target citation envelope")
    _update(
        source,
        "UPDATE claims SET confidence = 0.9, updated_at = ? WHERE id = ?",
        ("2030-01-01T00:00:00+00:00", source_claim.id),
    )
    _update(
        target,
        "UPDATE claims SET confidence = 0.2 WHERE id = ?",
        (target_claim.id,),
    )
    _update(
        target,
        "UPDATE citations SET locator = ? WHERE claim_id = ?",
        (ENCODED, target_claim.id),
    )

    merge_databases(str(target), str(source))

    assert _column(target, "claims", "confidence") == 0.2


def test_target_with_unsafe_citation_cannot_be_superseded(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    source_service = _service(source, tmp_path)
    target_service = _service(target, tmp_path)
    source_claim = source_service.ingest(
        "Remote citation conflict",
        [CitationInput(source="test://bridge")],
        source_agent="bridge-test",
        subject="bridge-citation",
        predicate="state",
        object_value="remote",
    )
    target_claim = target_service.ingest(
        "Local citation conflict",
        [CitationInput(source="test://bridge")],
        source_agent="bridge-test",
        subject="bridge-citation",
        predicate="state",
        object_value="local",
    )
    _update(
        source,
        "UPDATE claims SET pinned = 1, updated_at = ? WHERE id = ?",
        ("2030-01-01T00:00:00+00:00", source_claim.id),
    )
    _update(
        target,
        "UPDATE citations SET excerpt = ? WHERE claim_id = ?",
        (ENCODED, target_claim.id),
    )

    merge_databases(str(target), str(source))

    with sqlite3.connect(target) as conn:
        row = conn.execute(
            "SELECT status, replaced_by_claim_id FROM claims WHERE id = ?",
            (target_claim.id,),
        ).fetchone()
    assert row == ("candidate", None)


def test_duplicate_with_unsafe_citation_excerpt_cannot_reconcile(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    source_service = _service(source, tmp_path)
    target_service = _service(target, tmp_path)
    source_claim = _claim(source_service, "Shared unsafe citation excerpt")
    target_claim = _claim(target_service, "Shared unsafe citation excerpt")
    _update(
        source,
        "UPDATE claims SET confidence = 0.9, updated_at = ? WHERE id = ?",
        ("2030-01-01T00:00:00+00:00", source_claim.id),
    )
    _update(
        source,
        "UPDATE citations SET excerpt = ? WHERE claim_id = ?",
        (ENCODED, source_claim.id),
    )
    _update(
        target,
        "UPDATE claims SET confidence = 0.2 WHERE id = ?",
        (target_claim.id,),
    )

    merge_databases(str(target), str(source))

    assert _column(target, "claims", "confidence") == 0.2


def test_merge_rolls_back_claim_when_citation_insert_fails(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    source_service = _service(source, tmp_path)
    _service(target, tmp_path)
    _claim(source_service)
    with sqlite3.connect(target) as conn:
        conn.execute(
            """
            CREATE TRIGGER fail_bridge_citation
            BEFORE INSERT ON citations
            BEGIN
                SELECT RAISE(ABORT, 'fault injection');
            END
            """
        )

    stats = merge_databases(str(target), str(source))

    assert stats == {"scanned": 1, "merged": 0, "skipped": 0, "errors": 1}
    assert _column(target, "claims", "COUNT(*)") == 0
    assert _column(target, "citations", "COUNT(*)") == 0


def test_merge_never_logs_secret_bearing_sqlite_errors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    source_service = _service(source, tmp_path)
    _service(target, tmp_path)
    _claim(source_service)
    with sqlite3.connect(target) as conn:
        conn.execute(
            f"""
            CREATE TRIGGER fail_secret_echo
            BEFORE INSERT ON claims
            BEGIN
                SELECT RAISE(ABORT, '{LITERAL}');
            END
            """
        )

    with caplog.at_level(logging.WARNING):
        stats = merge_databases(str(target), str(source))

    assert stats["errors"] == 1
    assert LITERAL not in caplog.text


def test_duplicate_reconciliation_contains_secret_trigger_errors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    source_service = _service(source, tmp_path)
    target_service = _service(target, tmp_path)
    source_claim = _claim(source_service, "Duplicate trigger boundary")
    target_claim = _claim(target_service, "Duplicate trigger boundary")
    _update(
        source,
        "UPDATE claims SET confidence = 0.9, updated_at = ? WHERE id = ?",
        ("2030-01-01T00:00:00+00:00", source_claim.id),
    )
    _update(
        target,
        "UPDATE claims SET confidence = 0.2 WHERE id = ?",
        (target_claim.id,),
    )
    with sqlite3.connect(target) as conn:
        conn.execute(
            f"""
            CREATE TRIGGER fail_duplicate_reconciliation
            BEFORE UPDATE OF confidence ON claims
            BEGIN
                SELECT RAISE(ABORT, '{LITERAL}');
            END
            """
        )

    with caplog.at_level(logging.WARNING):
        stats = merge_databases(str(target), str(source))

    assert stats == {"scanned": 1, "merged": 0, "skipped": 0, "errors": 1}
    assert _column(target, "claims", "confidence") == 0.2
    assert LITERAL not in caplog.text


def test_duplicate_heavy_merge_commits_in_bounded_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _service(source, tmp_path)
    _service(target, tmp_path)
    _seed_duplicate_claims(source, 201, source=True)
    _seed_duplicate_claims(target, 201, source=False)

    class CountingConnection(sqlite3.Connection):
        commit_count = 0

        def commit(self) -> None:
            self.commit_count += 1
            super().commit()

    opened: list[CountingConnection] = []

    def open_target(path: str) -> CountingConnection:
        conn = sqlite3.connect(path, factory=CountingConnection)
        conn.row_factory = sqlite3.Row
        opened.append(conn)
        return conn

    monkeypatch.setattr(db_merge_module, "_open_target", open_target)

    stats = merge_databases(str(target), str(source))

    assert stats["skipped"] == 201
    assert opened[0].commit_count >= 2


def test_merge_rechecks_target_citations_after_batch_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _service(source, tmp_path)
    _service(target, tmp_path)
    _seed_duplicate_claims(source, 201, source=True)
    _seed_duplicate_claims(target, 201, source=False)
    with sqlite3.connect(target) as conn:
        conn.execute(
            """
            INSERT INTO citations (claim_id, source, locator, excerpt, created_at)
            VALUES (201, 'test://bridge', 'line-1', 'safe', ?)
            """,
            ("2020-01-01T00:00:00+00:00",),
        )

    class MutatingConnection(sqlite3.Connection):
        commit_count = 0

        def commit(self) -> None:
            super().commit()
            self.commit_count += 1
            if self.commit_count == 1:
                _update(
                    target,
                    "UPDATE citations SET locator = ? WHERE claim_id = 201",
                    (ENCODED,),
                )

    def open_target(path: str) -> MutatingConnection:
        conn = sqlite3.connect(path, factory=MutatingConnection)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(db_merge_module, "_open_target", open_target)

    stats = merge_databases(str(target), str(source))

    assert stats["skipped"] == 201
    with sqlite3.connect(target) as conn:
        confidence = conn.execute(
            "SELECT confidence FROM claims WHERE id = 201"
        ).fetchone()[0]
    assert confidence == 0.2


def test_merge_rechecks_target_identity_after_batch_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _service(source, tmp_path)
    _service(target, tmp_path)
    _seed_duplicate_claims(source, 201, source=True)
    _seed_duplicate_claims(target, 201, source=False)

    class MutatingConnection(sqlite3.Connection):
        commit_count = 0

        def commit(self) -> None:
            super().commit()
            self.commit_count += 1
            if self.commit_count == 1:
                _update(
                    target,
                    """
                    UPDATE claims
                    SET text = 'unrelated target claim',
                        idempotency_key = 'unrelated-target-key'
                    WHERE id = 201
                    """,
                    (),
                )

    def open_target(path: str) -> MutatingConnection:
        conn = sqlite3.connect(path, factory=MutatingConnection)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(db_merge_module, "_open_target", open_target)

    stats = merge_databases(str(target), str(source))

    with sqlite3.connect(target) as conn:
        unrelated_confidence = conn.execute(
            "SELECT confidence FROM claims WHERE id = 201"
        ).fetchone()[0]
        imported = conn.execute(
            "SELECT confidence FROM claims WHERE idempotency_key = 'duplicate-key-200'"
        ).fetchone()
        claim_count = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    assert stats["skipped"] == 200
    assert stats["merged"] == 1
    assert unrelated_confidence == 0.2
    assert imported == (0.9,)
    assert claim_count == 202


def test_merge_reads_claims_and_citations_from_one_source_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    source_service = _service(source, tmp_path)
    _service(target, tmp_path)
    claim = _claim(source_service, "Snapshot-safe merge citation")
    original = db_merge_module._source_citations
    changed = False

    def mutate_then_read(conn: sqlite3.Connection, claim_id: int):
        nonlocal changed
        if not changed:
            changed = True
            _update(
                source,
                "UPDATE citations SET source = ? WHERE claim_id = ?",
                (LITERAL, claim.id),
            )
        return original(conn, claim_id)

    monkeypatch.setattr(db_merge_module, "_source_citations", mutate_then_read)

    stats = merge_databases(str(target), str(source))

    assert stats["merged"] == 1
    assert _column(target, "citations", "source") == "test://bridge"


def test_merge_keeps_insert_and_conflict_resolution_in_one_transaction(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    source_service = _service(source, tmp_path)
    target_service = _service(target, tmp_path)
    source_service.ingest(
        "Remote bridge state",
        [CitationInput(source="test://bridge")],
        source_agent="bridge-test",
        subject="bridge",
        predicate="state",
        object_value="remote",
        confidence=0.9,
    )
    target_service.ingest(
        "Local bridge state",
        [CitationInput(source="test://bridge")],
        source_agent="bridge-test",
        subject="bridge",
        predicate="state",
        object_value="local",
        confidence=0.1,
    )
    with sqlite3.connect(target) as conn:
        conn.execute(
            f"""
            CREATE TRIGGER fail_bridge_conflict
            BEFORE UPDATE OF status ON claims
            WHEN NEW.status = 'superseded'
            BEGIN
                SELECT RAISE(ABORT, '{LITERAL}');
            END
            """
        )

    with caplog.at_level(logging.WARNING):
        stats = merge_databases(str(target), str(source))

    assert stats["errors"] == 1
    assert _column(target, "claims", "COUNT(*)") == 1
    assert LITERAL not in caplog.text


def test_redacted_merge_claim_cannot_supersede_a_safe_target_claim(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    source_service = _service(source, tmp_path)
    target_service = _service(target, tmp_path)
    source_claim = source_service.ingest(
        "Remote bridge state",
        [CitationInput(source="test://bridge")],
        source_agent="bridge-test",
        subject="bridge",
        predicate="state",
        object_value="remote",
    )
    target_claim = target_service.ingest(
        "Local bridge state",
        [CitationInput(source="test://bridge")],
        source_agent="bridge-test",
        subject="bridge",
        predicate="state",
        object_value="safe-local",
    )
    _update(
        source,
        "UPDATE claims SET object_value = ?, pinned = 1, updated_at = ? WHERE id = ?",
        (LITERAL, "2030-01-01T00:00:00+00:00", source_claim.id),
    )

    stats = merge_databases(str(target), str(source))

    assert stats["merged"] == 1
    with sqlite3.connect(target) as conn:
        row = conn.execute(
            "SELECT status, replaced_by_claim_id FROM claims WHERE id = ?",
            (target_claim.id,),
        ).fetchone()
    assert row == ("candidate", None)


def test_redacted_merge_claim_is_downgraded_to_unpinned_candidate(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    source_service = _service(source, tmp_path)
    _service(target, tmp_path)
    source_claim = _claim(source_service)
    _update(
        source,
        "UPDATE claims SET text = ?, status = 'confirmed', pinned = 1 WHERE id = ?",
        (LITERAL, source_claim.id),
    )

    stats = merge_databases(str(target), str(source))

    assert stats["merged"] == 1
    with sqlite3.connect(target) as conn:
        row = conn.execute("SELECT status, pinned FROM claims").fetchone()
    assert row == ("candidate", 0)


def test_delta_redaction_marker_remains_quarantined_after_merge(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    delta = tmp_path / "delta.db"
    target = tmp_path / "target.db"
    source_service = _service(source, tmp_path)
    _service(target, tmp_path)
    source_claim = _claim(source_service)
    _update(
        source,
        "UPDATE claims SET text = ?, status = 'confirmed', pinned = 1 WHERE id = ?",
        (LITERAL, source_claim.id),
    )

    export_delta(source, "", delta)
    stats = merge_databases(str(target), str(delta))

    assert stats["merged"] == 1
    with sqlite3.connect(target) as conn:
        row = conn.execute("SELECT status, pinned, text FROM claims").fetchone()
    assert row[0:2] == ("candidate", 0)
    assert "[REDACTED:" in row[2]
