"""Adversarial RED contracts for independent sensitivity-inventory review findings."""

from __future__ import annotations

import builtins
import io
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from memorymaster.govern.jobs import sensitivity_inventory
from memorymaster.govern.jobs.sensitivity_inventory import (
    run_inventory,
    scan_qdrant_payloads,
)
from scripts.sensitivity_inventory import main as inventory_main


def _synthetic_secret() -> str:
    return "sk-" + "R" * 24


def _empty_root(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    return root


def _empty_qdrant(**_kwargs: object) -> dict[str, object]:
    return {"result": {"points": [], "next_page_offset": None}}


def test_content_owning_fts5_values_are_scanned_before_completion(
    tmp_path: Path,
) -> None:
    db = tmp_path / "standalone-fts.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE VIRTUAL TABLE standalone_search USING fts5(body)")
        conn.execute(
            "INSERT INTO standalone_search(body) VALUES (?)",
            (_synthetic_secret(),),
        )
    artifact_root = _empty_root(tmp_path, "artifacts")
    spool_root = _empty_root(tmp_path, "spool")

    result = run_inventory(
        db,
        artifact_roots=[artifact_root],
        spool_roots=[spool_root],
        qdrant_page=_empty_qdrant,
    )

    sqlite_result = result["sqlite"]
    assert sqlite_result["records_scanned"] >= 1
    assert sqlite_result["records_flagged"] >= 1


def test_generated_column_does_not_drop_secret_tail_value(tmp_path: Path) -> None:
    db = tmp_path / "generated-column.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE claims (
                text TEXT,
                derived TEXT GENERATED ALWAYS AS (lower(text)) STORED,
                payload_json TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO claims(text, payload_json) VALUES (?, ?)",
            ("safe", json.dumps({"credential": _synthetic_secret()})),
        )
    artifact_root = _empty_root(tmp_path, "artifacts")
    spool_root = _empty_root(tmp_path, "spool")

    result = run_inventory(
        db,
        artifact_roots=[artifact_root],
        spool_roots=[spool_root],
        qdrant_page=_empty_qdrant,
    )

    payload_surface = result["sqlite"]["surfaces"]["claims.payload_json"]
    assert payload_surface["records_scanned"] == 1
    assert payload_surface["finding_counts"] == {"sensitive_value": 1}
    assert result["sqlite"]["records_flagged"] == 1


def test_long_postgres_secret_across_chunks_is_detected_or_blocks_coverage(
    tmp_path: Path,
) -> None:
    db = tmp_path / "safe.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE safe_table (value TEXT)")
    artifact_root = _empty_root(tmp_path, "artifacts")
    spool_root = _empty_root(tmp_path, "spool")
    password = "A" * 1024
    database_url = "postgres" + "ql://inventory:" + password + "@db.example.invalid/app"
    (artifact_root / "connection.txt").write_text(database_url, encoding="utf-8")

    result = run_inventory(
        db,
        artifact_roots=[artifact_root],
        spool_roots=[spool_root],
        qdrant_page=_empty_qdrant,
        chunk_size=64,
        max_file_bytes=4096,
    )

    artifacts = result["artifacts"]
    assert artifacts["status"] == "BLOCKED" or artifacts["sensitive_files"] == 1


def test_open_handle_identity_mismatch_is_refused_before_any_bytes_are_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _empty_root(tmp_path, "inside")
    inside = root / "candidate.txt"
    outside = tmp_path / "outside.txt"
    inside.write_text("safe", encoding="utf-8")
    outside.write_text(_synthetic_secret(), encoding="utf-8")

    class TrackingHandle:
        def __init__(self, target: Path) -> None:
            self._handle = builtins.open(target, "rb")
            self.bytes_read = 0

        def __enter__(self) -> TrackingHandle:
            return self

        def __exit__(self, *_args: object) -> None:
            self._handle.close()

        def fileno(self) -> int:
            return self._handle.fileno()

        def read(self, size: int = -1) -> bytes:
            data = self._handle.read(size)
            self.bytes_read += len(data)
            return data

    tracker = TrackingHandle(outside)
    original_open = Path.open
    inside_resolved = inside.resolve()

    def swapped_open(path: Path, *args: Any, **kwargs: Any) -> io.BufferedReader | TrackingHandle:
        if path == inside_resolved and args and args[0] == "rb":
            return tracker
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", swapped_open)

    result = sensitivity_inventory._file_inventory(
        [root],
        chunk_size=64,
        max_file_bytes=4096,
        max_entries=100,
    )

    assert result["status"] == "BLOCKED"
    assert result["files_scanned"] == 0
    assert tracker.bytes_read == 0


def test_qdrant_nested_bytes_and_missing_payload_fail_closed() -> None:
    nested_bytes = scan_qdrant_payloads(
        lambda **_kwargs: {
            "result": {
                "points": [{"payload": {"nested": {"value": _synthetic_secret().encode("utf-8")}}}],
                "next_page_offset": None,
            }
        }
    )
    missing_payload = scan_qdrant_payloads(
        lambda **_kwargs: {
            "result": {
                "points": [{"id": "synthetic-point"}],
                "next_page_offset": None,
            }
        }
    )

    assert nested_bytes == {
        "payloads_scanned": 1,
        "reason": "qdrant_payload_unscannable",
        "status": "BLOCKED-EXTERNAL",
        "surfaces": {
            "qdrant.payload": {
                "finding_counts": {"binary_opaque": 1},
                "records_flagged": 1,
                "records_scanned": 1,
            }
        },
    }
    assert missing_payload == {
        "reason": "qdrant_payload_missing",
        "status": "BLOCKED-EXTERNAL",
    }


def test_qdrant_hostile_scalar_subclasses_return_fixed_blockers() -> None:
    class HostileString(str):
        def encode(self, *_args: object, **_kwargs: object) -> bytes:
            raise RuntimeError("raw-" + _synthetic_secret())

    def payload(**_kwargs: object) -> dict[str, object]:
        return {
            "result": {
                "points": [{"payload": {"value": HostileString("safe")}}],
                "next_page_offset": None,
            }
        }

    def offset(**_kwargs: object) -> dict[str, object]:
        return {
            "result": {
                "points": [],
                "next_page_offset": HostileString("next"),
            }
        }

    assert scan_qdrant_payloads(payload)["reason"] == "qdrant_payload_unscannable"
    assert scan_qdrant_payloads(offset) == {
        "reason": "qdrant_malformed_offset",
        "status": "BLOCKED-EXTERNAL",
    }


def test_qdrant_requires_offset_key_and_preserves_partial_missing_payload_evidence() -> None:
    missing_offset = scan_qdrant_payloads(lambda **_kwargs: {"result": {"points": [{"payload": {"safe": True}}]}})
    partial = scan_qdrant_payloads(
        lambda **_kwargs: {
            "result": {
                "points": [
                    {"payload": {"token": _synthetic_secret()}},
                    {"id": "missing-payload"},
                ],
                "next_page_offset": None,
            }
        }
    )

    assert missing_offset == {
        "reason": "qdrant_malformed_response",
        "status": "BLOCKED-EXTERNAL",
    }
    assert partial["status"] == "BLOCKED-EXTERNAL"
    assert partial["reason"] == "qdrant_payload_missing"
    assert partial["payloads_scanned"] == 1
    assert partial["surfaces"]["qdrant.payload"]["finding_counts"] == {"sensitive_value": 1}


def test_huge_untyped_json_integer_returns_fixed_sqlite_blocker(
    tmp_path: Path,
) -> None:
    configured_limit = getattr(sys, "get_int_max_str_digits", lambda: 4300)()
    digit_count = max(5000, configured_limit + 100)
    db = tmp_path / "huge-integer.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE untyped_payload (value)")
        conn.execute("INSERT INTO untyped_payload(value) VALUES (?)", ("9" * digit_count,))
    artifact_root = _empty_root(tmp_path, "artifacts")
    spool_root = _empty_root(tmp_path, "spool")

    try:
        result = run_inventory(
            db,
            artifact_roots=[artifact_root],
            spool_roots=[spool_root],
            qdrant_page=_empty_qdrant,
        )
    except ValueError:
        pytest.fail("huge untyped JSON integers must return a fixed blocker, not raise")

    assert result["status"] == "BLOCKED"
    assert result["sqlite"]["status"] == "BLOCKED"
    assert result["sqlite"]["reason"] == "sqlite_value_unscannable"


def test_raw_and_parsed_json_are_both_scanned(tmp_path: Path) -> None:
    db = tmp_path / "duplicate-json-keys.sqlite"
    duplicate_key = '{"token":"' + _synthetic_secret() + '","token":"safe"}'
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE events (payload_json TEXT)")
        conn.executemany(
            "INSERT INTO events(payload_json) VALUES (?)",
            [('{"password":"WeakValue123"}',), (duplicate_key,)],
        )
    artifact_root = _empty_root(tmp_path, "artifacts")
    spool_root = _empty_root(tmp_path, "spool")

    result = run_inventory(
        db,
        artifact_roots=[artifact_root],
        spool_roots=[spool_root],
        qdrant_page=_empty_qdrant,
    )

    payload = result["sqlite"]["surfaces"]["events.payload_json"]
    assert payload["records_scanned"] == 2
    assert payload["records_flagged"] == 2
    assert payload["finding_counts"] == {"sensitive_value": 2}


def test_table_list_metadata_failure_has_specific_stable_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "table-list.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE safe_table (value TEXT)")
    artifact_root = _empty_root(tmp_path, "artifacts")
    spool_root = _empty_root(tmp_path, "spool")
    real_connect = sqlite3.connect

    class TableListUnavailableConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
            if sql.strip().casefold().startswith("pragma table_list"):
                raise sqlite3.OperationalError("synthetic table-list failure")
            return self._connection.execute(sql, *args)

        def close(self) -> None:
            self._connection.close()

    def connect_without_table_list(*args: Any, **kwargs: Any) -> TableListUnavailableConnection:
        return TableListUnavailableConnection(real_connect(*args, **kwargs))

    monkeypatch.setattr(sensitivity_inventory.sqlite3, "connect", connect_without_table_list)

    result = run_inventory(
        db,
        artifact_roots=[artifact_root],
        spool_roots=[spool_root],
        qdrant_page=_empty_qdrant,
    )

    assert result["sqlite"] == {
        "reason": "sqlite_table_list_unavailable",
        "status": "BLOCKED",
    }


def test_cli_invalid_integer_redacts_raw_marker_and_emits_json_blocker(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = _synthetic_secret()
    invalid_integer = "12" + marker
    db = tmp_path / "unused.sqlite"

    try:
        exit_code = inventory_main(["--db", str(db), "--chunk-size", invalid_integer])
    except SystemExit as exc:
        exit_code = int(exc.code)
    captured = capsys.readouterr()
    try:
        payload = json.loads(captured.out)
    except (json.JSONDecodeError, TypeError):
        payload = {}

    assert (marker in captured.out + captured.err, exit_code, payload.get("status")) == (
        False,
        3,
        "BLOCKED",
    )
    assert payload.get("classification") == "LEGACY-SENSITIVITY-INVENTORY"
    assert payload.get("mode") == "dry_run"
    assert payload.get("recommendation") == "REVIEW_ONLY"
