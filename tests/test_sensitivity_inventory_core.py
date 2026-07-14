"""Core SQLite end-to-end contract for the sensitivity inventory."""

from __future__ import annotations

import json
import sqlite3
from base64 import b64encode
from pathlib import Path

from memorymaster.govern.jobs.sensitivity_inventory import run_inventory

SECRET = "sk-" + "A" * 24
ENCODED_SECRET = b64encode(SECRET.encode()).decode()


def _database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE claims (id INTEGER PRIMARY KEY, text TEXT, metadata TEXT, embedding_json TEXT, blob BLOB);
        CREATE TABLE citations (id INTEGER PRIMARY KEY, excerpt TEXT);
        CREATE TABLE verbatim (id INTEGER PRIMARY KEY, body TEXT);
        CREATE TABLE atlas_evidence (id INTEGER PRIMARY KEY, payload TEXT);
        CREATE TABLE future_surface (id INTEGER PRIMARY KEY, unknown_value TEXT);
        CREATE TABLE "sk-AAAAAAAAAAAAAAAAAAAAAAAA" ("sk-AAAAAAAAAAAAAAAAAAAAAAAA" TEXT);
        CREATE VIRTUAL TABLE search_fts USING /* derived */ fts5(body);
        """
    )
    conn.execute(
        "INSERT INTO claims VALUES (1, ?, ?, ?, ?)",
        (
            SECRET,
            json.dumps({"api_key": ENCODED_SECRET}),
            "[1, 2.5]",
            SECRET.encode(),
        ),
    )
    conn.execute("INSERT INTO claims VALUES (2, 'safe', '{}', ?, ?)", ('["not numeric"]', bytes([255, 0])))
    conn.execute("INSERT INTO claims VALUES (3, 'safe', '{}', ?, ?)", ("[broken", b"safe"))
    conn.execute("INSERT INTO citations VALUES (9, ?)", (SECRET,))
    conn.execute("INSERT INTO verbatim VALUES (10, ?)", (SECRET,))
    conn.execute("INSERT INTO atlas_evidence VALUES (11, ?)", (json.dumps({"nested": SECRET}),))
    conn.execute("INSERT INTO future_surface VALUES (12, ?)", (SECRET,))
    conn.execute('INSERT INTO "sk-AAAAAAAAAAAAAAAAAAAAAAAA" VALUES (?)', ("safe",))
    conn.execute("INSERT INTO search_fts VALUES (?)", (SECRET,))
    conn.commit()
    conn.close()


def _serialized(result: dict[str, object]) -> str:
    return json.dumps(result, sort_keys=True)


def _assert_vocabulary_surfaces(sqlite: dict[str, object]) -> None:
    expected = (
        "claims.holder",
        "claims.source_agent",
        "claims.idempotency_key",
        "claims.scope",
        "claims.claim_type",
        "claims.visibility",
        "claims.tenant_id",
        "claims.created_at",
        "claims.wiki_path",
        "claims.field_json",
        "claims.field_numeric",
        "citations.source",
        "citations.locator",
        "events.from_status",
        "events.details",
        "events.payload_json",
        "events.content_hash",
        "events.hash_algorithm",
    )
    surfaces = sqlite["surfaces"]
    assert isinstance(surfaces, dict)
    assert all(surface in surfaces for surface in expected)


def _assert_core_inventory(
    result: dict[str, object],
    derived_objects: list[str],
    derived_records: int,
    tmp_path: Path,
) -> None:
    sqlite_result = result["sqlite"]
    assert isinstance(sqlite_result, dict)
    surfaces = sqlite_result["surfaces"]
    assert isinstance(surfaces, dict)
    assert result["mode"] == "dry_run"
    assert result["status"] == "COMPLETED"
    assert sqlite_result["records_scanned"] == 9
    assert sqlite_result["derived_records"] == derived_records
    assert sqlite_result["derived_tables_accounted"] == len(derived_objects)
    assert sqlite_result["tables_accounted"] == 7
    assert sqlite_result["columns_accounted"] == 15
    assert sqlite_result["schema_identifier_sensitive"] == 2
    assert surfaces["claims.text"] == {
        "finding_counts": {"sensitive_value": 1},
        "records_flagged": 1,
        "records_scanned": 3,
    }
    assert surfaces["claims.metadata"]["finding_counts"] == {"sensitive_value": 1}
    assert surfaces["claims.embedding_json"]["finding_counts"] == {
        "embedding_invalid": 1,
        "embedding_string": 1,
    }
    assert surfaces["claims.blob"]["finding_counts"] == {"binary_opaque": 1, "sensitive_value": 1}
    assert surfaces["unknown.field_text"]["records_scanned"] == 3
    serialized = _serialized(result)
    assert all(item not in serialized for item in (SECRET, ENCODED_SECRET, "legacy.sqlite", str(tmp_path)))
    assert '"9"' not in serialized and '"10"' not in serialized


def test_sqlite_inventory_is_dynamic_read_only_and_aggregate_only(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite"
    _database(db)
    before_mtime = db.stat().st_mtime_ns
    probe = sqlite3.connect(db)
    before_version = probe.execute("PRAGMA data_version").fetchone()[0]
    before_schema = probe.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    fts_objects = [
        row[0]
        for row in probe.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND (name = 'search_fts' OR name LIKE 'search_fts_%')"
        )
    ]
    derived_objects = [name for name in fts_objects if name != "search_fts"]
    derived_records = sum(
        int(probe.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in derived_objects
    )

    result = run_inventory(
        db,
        artifact_roots=[tmp_path],
        spool_roots=[tmp_path],
        qdrant_page=lambda **_kwargs: {"result": {"points": [], "next_page_offset": None}},
    )
    assert probe.execute("PRAGMA data_version").fetchone()[0] == before_version
    assert probe.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == before_schema
    probe.close()
    assert db.stat().st_mtime_ns == before_mtime
    _assert_core_inventory(result, derived_objects, derived_records, tmp_path)
    assert (
        run_inventory(
            db,
            artifact_roots=[tmp_path],
            spool_roots=[tmp_path],
            qdrant_page=lambda **_kwargs: {"result": {"points": [], "next_page_offset": None}},
        )
        == result
    )


def test_unique_row_flags_field_vocabulary_and_fts_indexes_derived(tmp_path: Path) -> None:
    db = tmp_path / "vocabulary.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE claims (holder TEXT, source_agent TEXT, idempotency_key TEXT, scope TEXT, claim_type TEXT, visibility TEXT, tenant_id TEXT, created_at TEXT, wiki_path TEXT, extra_json TEXT, extra_number INTEGER);
        CREATE TABLE citations (source TEXT, locator TEXT, excerpt TEXT);
        CREATE TABLE events (from_status TEXT, details TEXT, payload_json TEXT, content_hash TEXT, hash_algorithm TEXT);
        CREATE VIRTUAL TABLE search_fts USING fts5(body);
        CREATE VIRTUAL TABLE other_virtual USING rtree(id, x1, x2);
    """)
    conn.execute(
        "INSERT INTO claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (SECRET, SECRET, "safe", "safe", "fact", "safe", "safe", "safe", "safe", '{"safe":true}', 1),
    )
    conn.execute("INSERT INTO citations VALUES (?, ?, ?)", ("safe", "safe", SECRET))
    conn.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?)", ("safe", SECRET, "{}", "safe", "safe"))
    conn.execute("INSERT INTO search_fts VALUES (?)", ("safe",))
    conn.commit()
    derived = sorted(
        str(row[1]) for row in conn.execute("PRAGMA table_list") if str(row[0]) == "main" and str(row[2]) == "shadow"
    )
    derived_columns = sum(len(conn.execute(f'PRAGMA table_info("{name}")').fetchall()) for name in derived)
    derived_records = sum(int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in derived)
    conn.close()
    result = run_inventory(
        db,
        artifact_roots=[tmp_path],
        spool_roots=[tmp_path],
        qdrant_page=lambda **_: {"result": {"points": [], "next_page_offset": None}},
    )
    sqlite = result["sqlite"]
    assert sqlite["records_flagged"] == 3
    assert sqlite["derived_tables_accounted"] == len(derived)
    assert sqlite["derived_columns_accounted"] == derived_columns
    assert sqlite["derived_records"] == derived_records
    assert sqlite["tables_accounted"] == 5
    _assert_vocabulary_surfaces(sqlite)
