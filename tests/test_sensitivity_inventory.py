from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import subprocess
from base64 import b64encode
from pathlib import Path
from types import SimpleNamespace

import pytest

from memorymaster.govern.jobs.sensitivity_inventory import run_inventory, scan_qdrant_payloads
from memorymaster.govern.jobs import sensitivity_inventory


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


def test_artifact_and_spool_chunking_and_truncation_are_aggregate_only(tmp_path: Path) -> None:
    root = tmp_path / "inside"
    root.mkdir()
    (root / "a.txt").write_text("x" * 14 + "\n" + SECRET)
    (root / "large.txt").write_text("safe" * 100)
    result = run_inventory(None, artifact_roots=[root], spool_roots=[root], chunk_size=16, max_file_bytes=64)
    text = _serialized(result)
    assert result["artifacts"]["truncated_files"] >= 1
    assert result["artifacts"]["status"] == "BLOCKED"
    assert result["artifacts"]["reason"] == "file_truncated"
    assert result["artifacts"]["sensitive_files"] >= 1
    assert result["spool"]["status"] == "BLOCKED"
    assert result["spool"]["sensitive_files"] >= 1
    assert SECRET not in text and str(root) not in text and "a.txt" not in text


def test_artifact_symlink_escape_is_refused_when_supported(tmp_path: Path) -> None:
    root = tmp_path / "inside"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text(SECRET)
    link = root / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")

    result = run_inventory(None, artifact_roots=[root])
    assert result["artifacts"]["symlink_refused"] >= 1


def test_qdrant_payload_only_two_pages_and_blocker(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def page(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        if kwargs["offset"] is None:
            return {
                "result": {
                    "points": [{"id": "do-not-emit", "payload": {"token": SECRET}}],
                    "next_page_offset": "opaque-next",
                }
            }
        return {"result": {"points": [{"id": "do-not-emit-2", "payload": {"safe": "yes"}}], "next_page_offset": None}}

    scanned = scan_qdrant_payloads(page)
    root = tmp_path / "inputs"
    root.mkdir()
    db = tmp_path / "safe.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE safe_table (value TEXT)")
    blocked = run_inventory(db, artifact_roots=[root], spool_roots=[root])
    assert scanned["status"] == "COMPLETED"
    assert scanned["payloads_scanned"] == 2
    assert scanned["surfaces"]["qdrant.payload"]["finding_counts"]["sensitive_value"] == 1
    assert all(call["with_payload"] is True and call["with_vector"] is False for call in calls)
    assert [call["offset"] for call in calls] == [None, "opaque-next"]
    assert SECRET not in _serialized(scanned) and "do-not-emit" not in _serialized(scanned)
    assert blocked["qdrant"] == {"reason": "qdrant_not_configured", "status": "BLOCKED-EXTERNAL"}
    assert blocked["status"] == "BLOCKED-EXTERNAL"


def test_qdrant_repeated_offset_and_malformed_responses_are_bounded() -> None:
    repeated = scan_qdrant_payloads(
        lambda **_kwargs: {"result": {"points": [], "next_page_offset": "again"}}, max_pages=3
    )
    malformed = scan_qdrant_payloads(lambda **_kwargs: {"result": "bad"})
    assert repeated["status"] == "BLOCKED-EXTERNAL" and repeated["reason"] == "qdrant_repeated_offset"
    assert malformed == {"reason": "qdrant_malformed_response", "status": "BLOCKED-EXTERNAL"}


def test_fixed_categories_cover_legacy_families_without_schema_names(tmp_path: Path) -> None:
    db = tmp_path / "categories.sqlite"
    conn = sqlite3.connect(db)
    names = (
        "verbatim_memories",
        "usage_feedback",
        "external_sources",
        "source_items",
        "evidence_items",
        "action_proposals",
        "media_retry_queue",
        "query_cache",
        "cache_meta",
        "miner_state",
        "rule_records",
        "entity_records",
        "link_records",
        "timeline_entries",
    )
    for name in names:
        conn.execute(f'CREATE TABLE "{name}" (id INTEGER, value TEXT)')
        conn.execute(f'INSERT INTO "{name}" VALUES (1, "safe")')
    conn.commit()
    conn.close()
    result = run_inventory(
        db,
        artifact_roots=[tmp_path],
        spool_roots=[tmp_path],
        qdrant_page=lambda **_kwargs: {"result": {"points": [], "next_page_offset": None}},
    )
    surfaces = result["sqlite"]["surfaces"]
    for category in (
        "verbatim",
        "feedback",
        "atlas.source",
        "atlas.evidence",
        "atlas.action",
        "atlas.media_retry",
        "cache",
        "miner",
        "rules",
        "entities",
        "timeline",
    ):
        assert f"{category}.id" in surfaces
    assert all(name not in _serialized(result) for name in names)


@pytest.mark.parametrize(
    "sql",
    [
        'CREATE VIRTUAL TABLE x USING "fts5"(body)',
        "CREATE VIRTUAL TABLE x USING [fts5](body)",
        "CREATE VIRTUAL TABLE x USING /* comment */ fts5(body)",
    ],
)
def test_fts5_parser_accepts_quoted_and_commented_module_names(sql: str) -> None:
    assert sensitivity_inventory._is_fts5_sql(sql) is True


def test_fts5_comment_tokens_inside_quoted_name_keep_all_copies_derived(
    tmp_path: Path,
) -> None:
    db = tmp_path / "quoted-name.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE VIRTUAL TABLE "x--hidden" USING fts5(body)')
        conn.execute('INSERT INTO "x--hidden" VALUES ("safe")')
        objects = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND (name = 'x--hidden' OR name LIKE 'x--hidden_%')"
            )
        ]
    root = tmp_path / "root"
    root.mkdir()

    result = run_inventory(
        db,
        artifact_roots=[root],
        spool_roots=[root],
        qdrant_page=lambda **_kwargs: {"result": {"points": [], "next_page_offset": None}},
    )

    assert result["sqlite"]["derived_tables_accounted"] == len(objects) - 1
    assert result["sqlite"]["tables_accounted"] == 1
    assert result["sqlite"]["records_scanned"] == 1
    assert "x--hidden" not in _serialized(result)


def test_fts5_table_named_using_keeps_all_copies_derived(tmp_path: Path) -> None:
    db = tmp_path / "quoted-keyword.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE VIRTUAL TABLE "using" USING "fts5"(body)')
        conn.execute('INSERT INTO "using" VALUES ("safe")')
        objects = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND (name = 'using' OR name LIKE 'using_%')"
            )
        ]
    root = tmp_path / "root"
    root.mkdir()

    result = run_inventory(
        db,
        artifact_roots=[root],
        spool_roots=[root],
        qdrant_page=lambda **_kwargs: {"result": {"points": [], "next_page_offset": None}},
    )

    assert result["sqlite"]["derived_tables_accounted"] == len(objects) - 1
    assert result["sqlite"]["tables_accounted"] == 1
    assert result["sqlite"]["records_scanned"] == 1


def test_external_content_fts_does_not_hide_user_owned_suffix_table(
    tmp_path: Path,
) -> None:
    db = tmp_path / "external-content.sqlite"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE external_docs (
                rowid INTEGER PRIMARY KEY,
                body TEXT
            );
            CREATE TABLE docs_content (secret TEXT);
            CREATE VIRTUAL TABLE docs USING fts5(
                body,
                content='external_docs',
                content_rowid='rowid'
            );
            """
        )
        conn.execute("INSERT INTO docs_content VALUES (?)", (SECRET,))
    root = tmp_path / "root"
    root.mkdir()

    result = run_inventory(
        db,
        artifact_roots=[root],
        spool_roots=[root],
        qdrant_page=lambda **_kwargs: {"result": {"points": [], "next_page_offset": None}},
    )

    sqlite = result["sqlite"]
    assert sqlite["records_scanned"] == 1
    assert sqlite["records_flagged"] == 1
    assert sqlite["surfaces"]["unknown.field_text"]["finding_counts"] == {"sensitive_value": 1}
    assert "docs_content" not in _serialized(result)


def test_file_growth_after_size_check_is_incomplete() -> None:
    class TrackingBytesIO(io.BytesIO):
        bytes_read = 0

        def read(self, size: int = -1) -> bytes:
            data = super().read(size)
            self.bytes_read += len(data)
            return data

    class GrowingPath:
        suffix = ".txt"
        sizes = iter((4, 4, 4, 5, 5))
        handle = TrackingBytesIO(b"safe" + SECRET.encode())

        def stat(self) -> SimpleNamespace:
            return SimpleNamespace(st_size=next(self.sizes))

        def open(self, _mode: str) -> TrackingBytesIO:
            return self.handle

        def resolve(self, *, strict: bool) -> GrowingPath:
            assert strict is True
            return self

        def relative_to(self, _root: object) -> SimpleNamespace:
            return SimpleNamespace(parts=("safe.txt",))

    path = GrowingPath()
    _surface, reasons, _metadata, truncated = sensitivity_inventory._scan_file(
        path,
        object(),
        chunk_size=64,
        max_file_bytes=64,
    )

    assert "scan_incomplete" in reasons
    assert truncated is True
    assert path.handle.bytes_read <= 4


def _fake_stat(version: int) -> SimpleNamespace:
    return SimpleNamespace(st_size=4, st_mtime_ns=version, st_ctime_ns=version, st_dev=1, st_ino=1)


class _ChangingPath:
    suffix = ".txt"

    def __init__(self) -> None:
        self.stats = iter([_fake_stat(1), _fake_stat(1), _fake_stat(1), _fake_stat(2), _fake_stat(2)])

    def stat(self) -> SimpleNamespace:
        return next(self.stats)

    def open(self, _mode: str) -> io.BytesIO:
        return io.BytesIO(b"safe")

    def resolve(self, *, strict: bool) -> _ChangingPath:
        assert strict is True
        return self

    def relative_to(self, _root: object) -> SimpleNamespace:
        return SimpleNamespace(parts=("safe.txt",))


def test_equal_size_file_overwrite_is_incomplete() -> None:

    _surface, reasons, _metadata, truncated = sensitivity_inventory._scan_file(
        _ChangingPath(),
        object(),
        chunk_size=2,
        max_file_bytes=4,
    )

    assert "scan_incomplete" in reasons
    assert truncated is True


def test_file_metadata_root_blocking_and_refusal_are_aggregate_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "files"
    root.mkdir()
    encoded_name = b64encode(SECRET.encode()).decode() + ".json"
    (root / encoded_name).write_text('{"safe": true}')
    result = run_inventory(None, artifact_roots=[root], spool_roots=[tmp_path / "missing"])
    assert result["status"] == "BLOCKED"
    assert result["artifacts"]["surfaces"]["file.metadata"]["finding_counts"]["sensitive_value"] == 1
    assert result["artifacts"]["sensitive_files"] == 1
    assert result["spool"]["status"] == "BLOCKED"
    assert encoded_name not in _serialized(result)
    (root / "refused.txt").write_text("safe")
    (root / "kept.txt").write_text("safe")
    monkeypatch.setattr(
        sensitivity_inventory,
        "_linklike",
        lambda path: path.name == "refused.txt",
    )
    refused = run_inventory(None, artifact_roots=[root])
    assert refused["artifacts"]["files_refused"] == 1
    assert refused["artifacts"]["status"] == "COMPLETED"


def test_sqlite_accounts_views_and_schema_definition_secrets(tmp_path: Path) -> None:
    db = tmp_path / "views.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        f"""
        CREATE TABLE safe_table (
            value TEXT DEFAULT '{SECRET}'
        );
        INSERT INTO safe_table DEFAULT VALUES;
        CREATE VIEW safe_projection AS SELECT value FROM safe_table;
        """
    )
    conn.close()

    result = run_inventory(
        db,
        artifact_roots=[tmp_path],
        spool_roots=[tmp_path],
        qdrant_page=lambda **_kwargs: {"result": {"points": [], "next_page_offset": None}},
    )

    sqlite = result["sqlite"]
    assert sqlite["schema_definition_sensitive"] == 1
    assert sqlite["derived_views_accounted"] == 1
    assert sqlite["derived_records"] == 1
    assert SECRET not in _serialized(result)
    assert "safe_projection" not in _serialized(result)


def test_missing_db_invalid_file_bounds_and_root_refusal_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "inputs"
    root.mkdir()

    def qdrant(**_kwargs: object) -> dict[str, object]:
        return {"result": {"points": [], "next_page_offset": None}}

    missing_db = run_inventory(
        None,
        artifact_roots=[root],
        spool_roots=[root],
        qdrant_page=qdrant,
    )
    assert missing_db["status"] == "BLOCKED"
    assert missing_db["sqlite"] == {
        "reason": "sqlite_not_configured",
        "status": "BLOCKED",
    }

    db = tmp_path / "safe.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE safe_table (value TEXT)")
    invalid_bounds = run_inventory(
        db,
        artifact_roots=[root],
        spool_roots=[root],
        qdrant_page=qdrant,
        chunk_size=0,
    )
    assert invalid_bounds["status"] == "BLOCKED"
    assert invalid_bounds["artifacts"]["reason"] == "invalid_file_bounds"

    monkeypatch.setattr(sensitivity_inventory, "_linklike", lambda path: path == root)
    refused_root = run_inventory(
        db,
        artifact_roots=[root],
        spool_roots=[root],
        qdrant_page=qdrant,
    )
    assert refused_root["status"] == "BLOCKED"
    assert refused_root["artifacts"]["reason"] == "root_refused"


def test_file_entry_limit_blocks_with_deterministic_aggregate(tmp_path: Path) -> None:
    db = tmp_path / "safe.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE safe_table (value TEXT)")
    root = tmp_path / "many"
    root.mkdir()
    for index in range(5):
        (root / f"entry-{index}.txt").write_text("safe")

    result = run_inventory(
        db,
        artifact_roots=[root],
        spool_roots=[root],
        qdrant_page=lambda **_kwargs: {"result": {"points": [], "next_page_offset": None}},
        max_entries=2,
    )

    assert result["status"] == "BLOCKED"
    assert result["artifacts"]["reason"] == "entry_limit"
    assert result["artifacts"]["entries_accounted"] == 2
    assert result["artifacts"]["files_scanned"] == 0
    assert "entry-" not in _serialized(result)


def test_entry_limit_does_not_fetch_one_extra_directory_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    root.mkdir()

    class Entry:
        path = str(root / "unreadable")

    class CountingScan:
        calls = 0

        def __enter__(self) -> CountingScan:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self) -> CountingScan:
            return self

        def __next__(self) -> Entry:
            self.calls += 1
            return Entry()

    scan = CountingScan()
    monkeypatch.setattr(sensitivity_inventory.os, "scandir", lambda _root: scan)

    result = sensitivity_inventory._file_inventory(
        [root],
        chunk_size=16,
        max_file_bytes=64,
        max_entries=2,
    )

    assert result["reason"] == "entry_limit"
    assert scan.calls <= 2


def test_windows_reparse_attribute_is_linklike_without_path_is_junction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    class ReparseStat:
        st_file_attributes = reparse

    monkeypatch.setattr(Path, "is_symlink", lambda _path: False)
    monkeypatch.setattr(Path, "is_junction", lambda _path: False, raising=False)
    monkeypatch.setattr(Path, "lstat", lambda _path: ReparseStat())

    assert sensitivity_inventory._linklike(Path("junction")) is True


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_real_windows_junction_escape_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    spool = tmp_path / "spool"
    root.mkdir()
    outside.mkdir()
    spool.mkdir()
    (outside / "secret.txt").write_text(SECRET)
    junction = root / "escape"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        check=False,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip("junction creation unavailable")
    db = tmp_path / "safe.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE safe_table (value TEXT)")
    try:
        result = run_inventory(
            db,
            artifact_roots=[root],
            spool_roots=[spool],
            qdrant_page=lambda **_kwargs: {"result": {"points": [], "next_page_offset": None}},
        )
    finally:
        junction.rmdir()

    assert result["artifacts"]["files_refused"] == 1
    assert result["artifacts"]["files_scanned"] == 0
    assert result["artifacts"]["sensitive_files"] == 0
    assert SECRET not in _serialized(result)


def test_qdrant_fail_closed_bounds_keep_partial_aggregates() -> None:
    secret_error = "transport " + SECRET
    calls = 0

    def unhashable(**_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"result": {"points": [{"payload": {"token": SECRET}}], "next_page_offset": ["opaque"]}}
        return {"result": {"points": [], "next_page_offset": ["opaque"]}}

    repeated = scan_qdrant_payloads(unhashable, limit=1, max_pages=3)
    malformed_point = scan_qdrant_payloads(
        lambda **_kwargs: {"result": {"points": ["not-a-point"], "next_page_offset": None}}
    )
    limited = scan_qdrant_payloads(
        lambda **_kwargs: {"result": {"points": [], "next_page_offset": {"opaque": 1}}}, max_pages=1
    )
    failed = scan_qdrant_payloads(lambda **_kwargs: (_ for _ in ()).throw(RuntimeError(secret_error)))
    assert repeated["status"] == "BLOCKED-EXTERNAL" and repeated["payloads_scanned"] == 1
    assert malformed_point["reason"] == "qdrant_malformed_point" and malformed_point["status"] == "BLOCKED-EXTERNAL"
    assert limited["reason"] == "qdrant_page_limit" and limited["status"] == "BLOCKED-EXTERNAL"
    assert failed == {"reason": "qdrant_transport_error", "status": "BLOCKED-EXTERNAL"}
    assert SECRET not in _serialized(repeated) and secret_error not in _serialized(failed)
    assert (
        scan_qdrant_payloads(lambda **_kwargs: {"result": {"points": [], "next_page_offset": None}}, limit=0)["reason"]
        == "qdrant_invalid_bounds"
    )


def test_qdrant_rejects_non_json_offsets_oversized_pages_and_keeps_partial_error() -> None:
    class NonJsonOffset:
        pass

    non_json = scan_qdrant_payloads(lambda **_kwargs: {"result": {"points": [], "next_page_offset": NonJsonOffset()}})
    oversized = scan_qdrant_payloads(
        lambda **_kwargs: {
            "result": {
                "points": [{"payload": {}}, {"payload": {}}],
                "next_page_offset": None,
            }
        },
        limit=1,
    )
    calls = 0

    def partial_then_fail(**_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "result": {
                    "points": [{"payload": {"token": SECRET}}],
                    "next_page_offset": "next",
                }
            }
        raise RuntimeError("transport " + SECRET)

    partial = scan_qdrant_payloads(partial_then_fail)

    assert non_json == {
        "reason": "qdrant_malformed_offset",
        "status": "BLOCKED-EXTERNAL",
    }
    assert oversized["reason"] == "qdrant_page_oversized"
    assert partial["reason"] == "qdrant_transport_error"
    assert partial["payloads_scanned"] == 1
    assert partial["surfaces"]["qdrant.payload"]["records_flagged"] == 1
    assert SECRET not in _serialized(partial)

    deeply_nested: object = "offset"
    for _ in range(2000):
        deeply_nested = [deeply_nested]
    deep = scan_qdrant_payloads(lambda **_kwargs: {"result": {"points": [], "next_page_offset": deeply_nested}})
    assert deep["reason"] == "qdrant_malformed_offset"


def test_deep_values_and_hostile_qdrant_payloads_fail_closed(tmp_path: Path) -> None:
    deep_json = "[" * 2000 + "0" + "]" * 2000
    db = tmp_path / "deep.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE payloads (payload_json TEXT)")
        conn.execute("INSERT INTO payloads VALUES (?)", (deep_json,))
    artifact = tmp_path / "artifact"
    spool = tmp_path / "spool"
    artifact.mkdir()
    spool.mkdir()
    (artifact / "deep.json").write_text(deep_json)

    inventory = run_inventory(
        db,
        artifact_roots=[artifact],
        spool_roots=[spool],
        qdrant_page=lambda **_kwargs: {"result": {"points": [], "next_page_offset": None}},
    )

    assert inventory["status"] == "BLOCKED"
    assert inventory["sqlite"]["reason"] == "sqlite_value_unscannable"
    assert inventory["artifacts"]["reason"] == "file_unscannable"

    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    qdrant = scan_qdrant_payloads(
        lambda **_kwargs: {
            "result": {
                "points": [{"payload": cyclic}],
                "next_page_offset": None,
            }
        }
    )
    surrogate = scan_qdrant_payloads(lambda **_kwargs: {"result": {"points": [], "next_page_offset": "\ud800"}})

    assert qdrant["reason"] == "qdrant_payload_unscannable"
    assert surrogate["reason"] == "qdrant_malformed_offset"
    assert "self" not in _serialized(qdrant)


def test_cli_never_echoes_paths_or_raw_failures(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.sensitivity_inventory import main

    missing = tmp_path / "contains-secret-path"
    assert main(["--db", str(missing)]) == 3
    out = capsys.readouterr().out
    assert str(missing) not in out and "contains-secret-path" not in out
    assert json.loads(out)["status"] == "BLOCKED"
    db = tmp_path / "safe.sqlite"
    _database(db)
    roots = tmp_path / "roots"
    roots.mkdir()
    assert main(["--db", str(db), "--artifact-root", str(roots), "--spool-root", str(roots)]) == 4
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED-EXTERNAL"
    assert not hasattr(__import__("memorymaster.govern.jobs.sensitivity_inventory", fromlist=["*"]), "cleanup")
