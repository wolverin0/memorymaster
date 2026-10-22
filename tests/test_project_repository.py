from dataclasses import replace
from datetime import datetime, timezone
from importlib import import_module
import json
import os
import sqlite3

import pytest

from memorymaster.profile.project_projection import ProjectProjectionConfig
from memorymaster.profile.project_repository import load_project_snapshot, snapshot_since


@pytest.mark.parametrize("field", ["text", "subject", "citation"])
@pytest.mark.parametrize("gap", ["  ", "\n"])
def test_generated_marker_variants_never_qualify(project_config, field, gap):
    value = f"<!-- generated{gap}by MemoryMaster; rebuild instead of editing -->generated<!-- /generated{gap}by MemoryMaster -->"
    claim(project_config, **({field: value} if field != "citation" else {}))
    if field == "citation":
        with sqlite3.connect(project_config.db_path) as connection:
            connection.execute("UPDATE citations SET excerpt=?", (value,))
    assert "claim:1" not in load_project_snapshot(project_config)["items"]


@pytest.fixture
def project_config(tmp_path):
    root = tmp_path / "demo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    db = tmp_path / "fixture.sqlite"
    with sqlite3.connect(db) as conn:
        conn.executescript("""CREATE TABLE claims(id INTEGER PRIMARY KEY,text TEXT,
            scope TEXT,tenant_id TEXT,status TEXT,visibility TEXT,source_agent TEXT,
            replaced_by_claim_id INTEGER,version INTEGER DEFAULT 1,claim_type TEXT,
            subject TEXT,predicate TEXT,object_value TEXT,pinned INTEGER DEFAULT 0,
            created_at TEXT,updated_at TEXT,event_time TEXT,valid_from TEXT,valid_until TEXT);
            CREATE TABLE citations(id INTEGER PRIMARY KEY,claim_id INTEGER,source TEXT,
            locator TEXT,excerpt TEXT,created_at TEXT);""")
        import_module("memorymaster.stores.migrations.0029_project_source_cursor").apply_sqlite(conn)
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    (ledger / "tasks.json").write_text(json.dumps({"complete": True, "tasks": []}))
    return ProjectProjectionConfig(
        db_path=db,
        scope="project:demo",
        tenant_id=None,
        context_key="fixture",
        epoch="pilot",
        project_root=root,
        output_root=tmp_path / "out",
        state_root=tmp_path / "state",
        ledger_root=ledger,
        ledger_repo="demo",
        ledger_filtered=True,
        enabled=True,
    )


def claim(config, id=1, text="Use bounded reads", **changes):
    values = dict(
        id=id,
        text=text,
        scope="project:demo",
        tenant_id=None,
        status="confirmed",
        visibility="public",
        source_agent="operator",
        version=1,
        claim_type="decision",
        created_at="2026-10-01T00:00:00+00:00",
        updated_at="2026-10-01T00:00:00+00:00",
    )
    values.update(changes)
    with sqlite3.connect(config.db_path) as conn:
        conn.execute(
            f"INSERT INTO claims ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values())
        )
        conn.execute(
            "INSERT INTO citations(claim_id,source,locator,excerpt) VALUES (?,?,?,?)",
            (id, "session", f"session:{id}", "Authored evidence"),
        )


def test_confirmed_authorized_current_selection(project_config):
    config = project_config
    claim(config)
    for i, changes in enumerate(
        [
            {"status": "candidate"},
            {"scope": "project:other"},
            {"tenant_id": "other"},
            {"visibility": "private"},
            {"valid_until": "2001-01-01"},
            {"valid_from": "2999-01-01"},
            {"replaced_by_claim_id": 1},
            {"source_agent": "project-projection"},
            {"subject": "password=secret-password"},
        ],
        2,
    ):
        claim(config, i, **changes)
    claim(config, 20, subject="engine", predicate="uses", object_value=None)
    result = load_project_snapshot(config)
    assert set(result["items"]) == {"claim:1", "claim:20"}
    assert result["complete"]
    assert result["coverage"]["claim:1"]["kind"] == "claim"


def test_ledger_actual_schema_visibility_and_completeness(project_config):
    config = project_config
    path = config.ledger_root / "tasks.json"
    path.write_text(
        json.dumps(
            {
                "complete": True,
                "tasks": [
                    {"id": "T-1", "repo": "demo", "state": "ready", "title": "Visible"},
                    {"id": "T-2", "repo": "demo", "state": "blocked", "title": "Hidden", "blind": True},
                    {"id": "T-3", "repo": "other", "state": "ready", "title": "Other"},
                    {"id": "T-4", "repo": "demo", "state": "done", "title": "Closed"},
                ],
            }
        )
    )
    result = load_project_snapshot(config)
    assert set(result["items"]) == {"card:T-1", "card:T-4"}
    assert set(result["coverage"]) == {"card:T-1", "__scan_fence__"}
    path.write_text("{")
    result = load_project_snapshot(config)
    assert not result["complete"]
    assert "ledger_unavailable" in result["diagnostics"]


def test_incremental_baseline_rehydrates_coverage_and_changed_rows(project_config):
    config = project_config
    claim(config)
    baseline = load_project_snapshot(config)
    claim(config, 2, status="candidate")
    with sqlite3.connect(config.db_path) as conn:
        conn.execute("UPDATE claims SET status='confirmed',updated_at=? WHERE id=2", (baseline["read_at"],))
        conn.execute("UPDATE citations SET excerpt='Edited evidence' WHERE claim_id=1")
    current = snapshot_since(config, baseline["coverage"], baseline["read_at"])
    assert set(current["coverage"]) == {"claim:1", "claim:2", "__scan_fence__"}
    assert current["coverage"]["claim:1"] != baseline["coverage"]["claim:1"]


def test_explicit_context_and_disabled_have_no_source_reads(project_config):
    config = replace(project_config, enabled=False, db_path=project_config.db_path / "missing")
    assert load_project_snapshot(config)["diagnostics"] == ["inactive"]
    with pytest.raises(ValueError):
        replace(project_config, context_key="../escape")
    with pytest.raises(ValueError):
        replace(project_config, db_path="postgresql://localhost/db")


def test_oversized_citation_cannot_hide_sensitive_or_generated_payload(project_config):
    claim(project_config)
    with sqlite3.connect(project_config.db_path) as conn:
        conn.execute(
            "INSERT INTO citations(claim_id,source,locator,excerpt) VALUES (1,?,?,?)",
            ("session", "extra", "x" * 17000 + "<!-- generated by MemoryMaster -->"),
        )
    snapshot = load_project_snapshot(project_config)
    assert "claim:1" not in snapshot["items"]
    assert not snapshot["complete"]


def test_incremental_includes_temporal_boundary_without_row_edit(project_config, monkeypatch):
    from memorymaster.profile import project_repository as repository

    now = "2026-10-22T09:00:00+00:00"
    original_current = repository.claim_is_temporally_current
    monkeypatch.setattr(repository, "utc_now", lambda: now)
    monkeypatch.setattr(repository, "claim_is_temporally_current", lambda row: original_current(
        row, now=datetime.fromisoformat(now).astimezone(timezone.utc)))
    claim(project_config, valid_from="2026-10-22T09:01:00+00:00")
    claim(project_config, 2, valid_until="2026-10-22T09:01:00+00:00")
    baseline = load_project_snapshot(project_config)
    assert "claim:1" not in baseline["coverage"] and "claim:2" in baseline["coverage"]
    now = "2026-10-22T09:02:00+00:00"
    snapshot = snapshot_since(project_config, baseline["coverage"], baseline["read_at"])
    assert "claim:1" in snapshot["coverage"]
    assert "claim:2" not in snapshot["coverage"]
    assert snapshot["complete"]
    assert "next_temporal_boundary" in snapshot


def test_incremental_same_second_promotions_and_new_rows_use_inclusive_floor(project_config, monkeypatch):
    from memorymaster.profile import project_repository as repository

    claim(project_config, status="candidate")
    monkeypatch.setattr(repository, "utc_now", lambda: "2026-10-22T15:00:00.750000+00:00")
    baseline = load_project_snapshot(project_config)
    assert not any(key.startswith("claim:") for key in baseline["coverage"])
    # Store writes have whole-second UTC timestamps, while projection read_at
    # retains microseconds. Both commits happen after the baseline read.
    store_time = "2026-10-22T15:00:00+00:00"
    with sqlite3.connect(project_config.db_path) as conn:
        conn.execute("UPDATE claims SET status='confirmed',updated_at=? WHERE id=1", (store_time,))
    claim(project_config, 2, created_at=store_time, updated_at=store_time)
    current = snapshot_since(project_config, baseline["coverage"], baseline["read_at"])
    assert set(current["coverage"]) == {"claim:1", "claim:2", "__scan_fence__"}


@pytest.mark.parametrize("rewrite_count", [1, 2])
def test_ledger_digest_detects_equal_stat_rewrites(project_config, monkeypatch, rewrite_count):
    from memorymaster.profile import project_repository as repository

    path = project_config.ledger_root / "tasks.json"

    def encoded(title):
        return json.dumps(
            {"complete": True, "tasks": [{"id": "T-1", "repo": "demo", "state": "ready", "title": title}]}
        ).encode()

    path.write_bytes(encoded("Before"))
    original_stat = path.stat()
    original_loads = json.loads
    calls = []

    def racing_loads(raw, *args, **kwargs):
        result = original_loads(raw, *args, **kwargs)
        if isinstance(result, dict) and "tasks" in result:
            calls.append(True)
            if len(calls) <= rewrite_count:
                path.write_bytes(encoded("After!" if len(calls) == 1 else "Before"))
                os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        return result

    monkeypatch.setattr(repository.json, "loads", racing_loads)
    current = load_project_snapshot(project_config)
    assert len(calls) == 2
    if rewrite_count == 1:
        assert current["complete"]
        assert "After!" in current["items"]["card:T-1"]["text"]
    else:
        assert not current["complete"]
        assert "card:T-1" not in current["items"]


def test_incremental_fence_finds_late_old_timestamp_promotions_and_inserts(project_config):
    claim(project_config, status="candidate")
    baseline = load_project_snapshot(project_config)
    assert baseline["coverage"]["__scan_fence__"]["watched_ids"] == [1]
    # These timestamps can precede read_at in a long transaction, despite the
    # commits becoming visible only after the projection snapshot.
    with sqlite3.connect(project_config.db_path) as conn:
        conn.execute("UPDATE claims SET status='confirmed' WHERE id=1")
    claim(project_config, 2, updated_at="2001-01-01T00:00:00+00:00")
    current = snapshot_since(project_config, baseline["coverage"], baseline["read_at"])
    assert {"claim:1", "claim:2"} <= set(current["coverage"])
    assert current["complete"]
    assert current["coverage"]["__scan_fence__"]["watched_ids"] == [1, 2]


def test_incremental_fence_finds_first_citation_without_claim_timestamp_edit(project_config):
    claim(project_config)
    with sqlite3.connect(project_config.db_path) as conn:
        conn.execute("DELETE FROM citations")
    baseline = load_project_snapshot(project_config)
    assert "claim:1" not in baseline["coverage"]
    with sqlite3.connect(project_config.db_path) as conn:
        conn.execute(
            "INSERT INTO citations(claim_id,source,locator,excerpt) VALUES (1,'session','authored','evidence')"
        )
    current = snapshot_since(project_config, baseline["coverage"], baseline["read_at"])
    assert "claim:1" in current["coverage"]
    assert current["complete"]


@pytest.mark.parametrize("mutation", [
    "DELETE FROM claims WHERE id=1",
    "DELETE FROM citations WHERE claim_id=1",
    "UPDATE claims SET scope='project:other' WHERE id=1",
    "UPDATE claims SET tenant_id='other' WHERE id=1",
    "UPDATE claims SET id=3 WHERE id=1",
])
def test_incremental_withdraws_deleted_or_unauthorized_supports(project_config, mutation):
    claim(project_config)
    claim(project_config, 2)
    baseline = load_project_snapshot(project_config)
    with sqlite3.connect(project_config.db_path) as connection:
        connection.execute(mutation)
    current = snapshot_since(project_config, baseline["coverage"], baseline["read_at"])
    assert current["complete"]
    assert "claim:1" not in current["coverage"]
    assert current["coverage"]["claim:2"] == baseline["coverage"]["claim:2"]


@pytest.mark.parametrize("invalid_cursor", [None, {"epoch": "another-database", "seq": 0},
                                           {"epoch": "same", "seq": 1000000}])
def test_incremental_missing_or_rewound_cursor_fails_closed_without_source_scan(project_config, monkeypatch, invalid_cursor):
    from memorymaster.profile import project_repository as repository

    claim(project_config)
    baseline = load_project_snapshot(project_config)
    if invalid_cursor and invalid_cursor["epoch"] == "same":
        invalid_cursor = {**invalid_cursor, "epoch": baseline["coverage"]["__scan_fence__"]["source_cursor"]["epoch"]}
    baseline["coverage"]["__scan_fence__"]["source_cursor"] = invalid_cursor
    statements = []
    original_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(repository.sqlite3, "connect", traced_connect)
    current = snapshot_since(project_config, baseline["coverage"], baseline["read_at"])
    assert not current["complete"]
    assert not any("FROM claims" in statement or "FROM citations" in statement for statement in statements)


def test_incremental_coverage_requires_read_watermark(project_config):
    claim(project_config)
    baseline = load_project_snapshot(project_config)
    current = load_project_snapshot(project_config, coverage=baseline["coverage"])
    assert not current["complete"]
    assert "__scan_fence__" not in current["coverage"]
    assert not current["items"]


def test_incomplete_scan_fence_never_claims_complete_incremental_coverage(project_config, monkeypatch):
    from memorymaster.profile import project_repository as repository

    monkeypatch.setattr(repository, "MAX_CLAIMS", 1)
    claim(project_config, status="candidate")
    claim(project_config, 2, status="candidate")
    baseline = load_project_snapshot(project_config)
    assert not baseline["complete"]
    assert not baseline["coverage"]["__scan_fence__"]["complete"]
    current = snapshot_since(project_config, baseline["coverage"], baseline["read_at"])
    assert not current["complete"]


def test_incremental_fence_survives_real_wal_late_commit_without_full_rescan(project_config, monkeypatch):
    from memorymaster.profile import project_repository as repository

    claim(project_config, status="candidate")
    writer = sqlite3.connect(project_config.db_path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE claims SET status='confirmed',updated_at='2001-01-01T00:00:00+00:00' WHERE id=1")
        writer.execute(
            "INSERT INTO claims(id,text,scope,status,visibility,source_agent,claim_type,created_at,updated_at) "
            "VALUES (2,'Late insert','project:demo','confirmed','public','operator','decision',"
            "'2001-01-01T00:00:00+00:00','2001-01-01T00:00:00+00:00')"
        )
        writer.execute(
            "INSERT INTO citations(claim_id,source,locator,excerpt) VALUES (2,'session','authored','evidence')"
        )
        baseline = load_project_snapshot(project_config)
        assert baseline["coverage"]["__scan_fence__"]["watched_ids"] == [1]
        assert "claim:1" not in baseline["coverage"]
        writer.commit()
    finally:
        writer.close()
    statements = []
    original_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(repository.sqlite3, "connect", traced_connect)
    current = snapshot_since(project_config, baseline["coverage"], baseline["read_at"])
    assert {"claim:1", "claim:2"} <= set(current["coverage"])
    assert current["complete"]
    row_reads = [statement for statement in statements if "_source_bytes FROM claims" in statement]
    assert len(row_reads) == 1
    assert "id IN (1,2)" in row_reads[0]
    selection = next(statement for statement in statements if "FROM project_source_changes" in statement)
    assert "changes.seq >" in selection and "id > 1" in selection


@pytest.mark.parametrize("source", ["claims", "citations"])
def test_source_cursor_stops_before_materializing_over_byte_budget(project_config, monkeypatch, source):
    from memorymaster.profile import project_repository as repository

    claim(project_config, text="safe")
    with sqlite3.connect(project_config.db_path) as conn:
        if source == "claims":
            for id in range(2, 102):
                conn.execute(
                    "INSERT INTO claims(id,text,scope,status) VALUES (?,?,'project:demo','candidate')",
                    (id, "bounded " * 150),
                )
        else:
            conn.executemany(
                "INSERT INTO citations(claim_id,source,locator,excerpt) VALUES (1,'session','authored',?)",
                [("evidence " * 200,)] * 100,
            )
    consumed = []

    class TrackingCursor(sqlite3.Cursor):
        def execute(self, query, parameters=()):
            self.query = query
            return super().execute(query, parameters)

        def fetchall(self):
            rows = super().fetchall()
            if f"FROM {source}" in self.query:
                consumed.extend(rows)
            return rows

        def __next__(self):
            row = super().__next__()
            if f"FROM {source}" in self.query:
                consumed.append(row)
            return row

    class TrackingConnection(sqlite3.Connection):
        def execute(self, query, parameters=()):
            return self.cursor(factory=TrackingCursor).execute(query, parameters)

    original_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        return original_connect(*args, **kwargs, factory=TrackingConnection)

    monkeypatch.setattr(repository.sqlite3, "connect", tracking_connect)
    monkeypatch.setattr(repository, "MAX_SOURCE_BYTES", 5000)
    snapshot = load_project_snapshot(project_config)
    assert not snapshot["complete"]
    assert len(consumed) < 10


def test_oversized_multibyte_rows_are_not_materialized_before_field_rejection(project_config, monkeypatch):
    from memorymaster.profile import project_repository as repository

    claim(project_config)
    with sqlite3.connect(project_config.db_path) as conn:
        conn.execute("UPDATE claims SET text=?,subject=?,object_value=? WHERE id=1", ("😀" * 18000,) * 3)
    observed = []
    original_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        conn.set_trace_callback(observed.append)
        return conn

    monkeypatch.setattr(repository.sqlite3, "connect", traced_connect)
    snapshot = load_project_snapshot(project_config)
    assert not snapshot["complete"]
    assert snapshot["coverage"]["__scan_fence__"]["watched_ids"] == [1]
    query = next(query for query in observed if "FROM claims WHERE" in query)
    assert "CASE WHEN" in query and "ELSE NULL END AS text" in query
