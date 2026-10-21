from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from memorymaster.dreaming.agy_source import (
    AgySourceError, discover_agy_sessions, discover_session_bundles,
    iter_agy_records, read_executor_metadata,
)
from memorymaster.dreaming.models import DreamCandidate
from memorymaster.dreaming.source_review import CHECKS, bind_source, parse_review


def _varint(value):
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def _field(number, data):
    return _varint(number * 8 + 2) + _varint(len(data)) + data


def _metadata(cwd, model="gemini-test"):
    nested = _field(42, _field(11, _field(1, cwd.encode()))) + _field(28, model.encode())
    return _field(10, _field(1, nested))


def _db(path, blobs):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE executor_metadata (idx INTEGER, data BLOB)")
        conn.executemany("INSERT INTO executor_metadata VALUES (?, ?)", enumerate(blobs))


def _rows(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _row(step, kind="PLANNER_RESPONSE", **kwargs):
    return {"step_index": step, "type": kind, "source": "USER_EXPLICIT" if kind == "USER_INPUT" else "MODEL",
            "status": "DONE", "created_at": "2026-10-19T10:00:00Z", **kwargs}


def test_metadata_reader_is_readonly_and_never_changes_native_store(tmp_path, monkeypatch):
    database = tmp_path / "fixture.db"
    _db(database, [_metadata(str(tmp_path / "repo"))])
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    connect = sqlite3.connect
    calls = []
    writes = []

    def guarded_connect(path, **kwargs):
        assert str(path).endswith("?mode=ro") and kwargs["uri"] is True
        conn = connect(path, **kwargs)

        def authorize(action, arg1, arg2, db, trigger):
            if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                          sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE}:
                writes.append(action)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        conn.set_trace_callback(calls.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    result = read_executor_metadata(database)
    assert result.status == "resolved" and result.cwd == str(tmp_path / "repo")
    assert result.model_id == "gemini-test"
    assert any("query_only = ON" in statement for statement in calls)
    assert writes == []
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert not database.with_name(database.name + "-journal").exists()


def test_missing_invalid_and_conflicting_metadata_are_unresolved(tmp_path):
    assert read_executor_metadata(tmp_path / "missing.db").reason == "missing_companion"
    broken = tmp_path / "broken.db"
    _db(broken, [b"\x00"])
    assert read_executor_metadata(broken).reason == "decoder_failure"
    conflict = tmp_path / "conflict.db"
    _db(conflict, [_metadata(str(tmp_path / "a")), _metadata(str(tmp_path / "b"))])
    assert read_executor_metadata(conflict).reason == "conflicting_cwd"
    assert read_executor_metadata(conflict).cwd is None


def test_latest_revision_full_replacement_thinking_errors_and_tool_exclusion(tmp_path):
    compact, full = tmp_path / "transcript.jsonl", tmp_path / "transcript_full.jsonl"
    _rows(compact, [
        _row(0, "USER_INPUT", content="Choose SQLite."),
        _row(1, content="superseded running response", status="RUNNING"),
        _row(1, content="Short reply", thinking="reasoning " * 400, truncated_fields=["content"]),
        _row(2, "GENERIC", content="sensitive tool output", error="Historical socket timeout"),
        _row(3, "ERROR_MESSAGE", content="Provider failed"),
        _row(4, "SYSTEM_MESSAGE", content="system instructions"),
    ])
    _rows(full, [_row(1, content="The selected reply", thinking="reasoning " * 400)])
    records = list(iter_agy_records(compact))
    assert [record.step_index for record in records] == [0, 1, 2, 3]
    assert all(record.message.role in {"user", "assistant"} for record in records)
    assert "[thinking]" in records[1].message.text
    assert records[1].message.text.split("[thinking] ")[1] == ("reasoning " * 400)[:2000].strip()
    assert "full_field_replacement" in records[1].provenance
    assert "superseded" not in repr(records)
    assert "sensitive tool output" not in repr(records)
    assert records[2].message.text == "[error] Historical socket timeout"
    assert records[3].message.text == "[error] Provider failed"
    assert records[1].end_offset > records[1].start_offset


def test_missing_companion_and_malformed_records_are_explicit(tmp_path):
    compact = tmp_path / "transcript.jsonl"
    _rows(compact, [_row(0, content="cut", truncated_fields=["content"])])
    with pytest.raises(AgySourceError, match="missing_full_fields"):
        list(iter_agy_records(compact))
    compact.write_bytes(b"not json\n")
    with pytest.raises(AgySourceError, match="malformed_record"):
        list(iter_agy_records(compact))


def test_freeze_ignores_append_and_partial_tail(tmp_path):
    compact = tmp_path / "transcript.jsonl"
    _rows(compact, [_row(0, "USER_INPUT", content="Freeze this requirement.")])
    frozen = compact.stat().st_size
    with compact.open("ab") as handle:
        handle.write(json.dumps(_row(1, content="Later append")).encode() + b"\n")
        handle.write(b'{"step_index":2')
    assert [record.step_index for record in iter_agy_records(compact, frozen_end=frozen)] == [0]
    assert [record.step_index for record in iter_agy_records(compact)] == [0, 1]


@pytest.mark.parametrize("field,provenance", [("thinking", "[thinking]"), ("error", "[error]")])
def test_normalized_reasoning_and_errors_can_receive_source_review(tmp_path, field, provenance):
    compact = tmp_path / "transcript.jsonl"
    _rows(compact, [_row(0, **{field: "SQLite failed because the WAL file was missing."})])
    message = next(iter_agy_records(compact)).message
    assert message.role == "assistant" and message.text.startswith(provenance)
    candidate = DreamCandidate("c1", "SQLite needs its WAL file for recovery.", "lesson", "sqlite", "requires", "WAL", "project",
                               message.message_id, message.text, .9)
    bound = bind_source(candidate, {"scope": "project:fixture", "provider": "agy", "session_hash": "fixture", "messages": [message.to_dict()]})
    review = {"version": 2, "source_hash": bound.source_context["source_hash"], "verdict": "accept",
              "checks": dict.fromkeys(CHECKS, True), "selection": {"destination": "memory", "kind": "lesson", "novelty": "new",
              "scope": "project:fixture", "memory_key": "sqlite:wal-recovery", "future_use": "When recovering SQLite after a failure"}}
    assert parse_review(review, bound)["verdict"] == "accept"


def test_shared_sanitizer_applies_to_thinking_and_error_fields(tmp_path):
    compact = tmp_path / "transcript.jsonl"
    _rows(compact, [_row(0, thinking="[MemoryMaster recall] prior claim"), _row(1, error="Claim mm-12345678 failed")])
    assert list(iter_agy_records(compact)) == []


def test_sensitive_fields_are_redacted_without_modifying_either_source(tmp_path):
    compact, full = tmp_path / "transcript.jsonl", tmp_path / "transcript_full.jsonl"
    secret = "sk-LiveSecret1234567890abcd"
    _rows(compact, [_row(0, content="cut", truncated_fields=["content"]), _row(1, error=f"Failure using {secret}")])
    _rows(full, [_row(0, content=f"Use {secret} from C:\\Users\\fixture\\token.txt")])
    before = [path.read_bytes() for path in (compact, full)]
    records = list(iter_agy_records(compact))
    assert secret not in repr(records)
    assert "C:\\Users\\fixture" not in repr(records)
    assert "[REDACTED:" in repr(records)
    assert before == [path.read_bytes() for path in (compact, full)]


def test_discovery_groups_subagents_and_ignores_chunk_copies(tmp_path):
    project = tmp_path / "claude" / "encoded-project"
    children = project / "parent-session" / "subagents"
    children.mkdir(parents=True)
    (project / "parent-session.jsonl").write_text("", encoding="utf-8")
    (children / "agent-one.jsonl").write_text("", encoding="utf-8")
    bundles = list(discover_session_bundles(tmp_path / "claude"))
    assert len(bundles) == 1 and len(bundles[0].streams) == 2
    assert len(bundles[0].child_streams) == 1
    uuid = "12345678-1234-1234-1234-123456789abc"
    logs = tmp_path / "agy" / "brain" / uuid / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    (logs / "transcript.jsonl").write_text("", encoding="utf-8")
    (logs / "transcript_full.jsonl").write_text("", encoding="utf-8")
    chunks = logs / "chunks" / "transcript"
    chunks.mkdir(parents=True)
    (chunks / "part.jsonl").write_text("", encoding="utf-8")
    sessions = list(discover_agy_sessions(tmp_path / "agy"))
    assert len(sessions) == 1 and sessions[0].database_path is None
    assert sessions[0].full_path.name == "transcript_full.jsonl"
