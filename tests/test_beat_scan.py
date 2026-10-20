"""AO-2 v1 beat: incremental transcript scanner (T-1640 AC1). Hermetic: tmp dirs only."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memorymaster.dreaming.capture import capture_hook_payload, scan_active_transcripts
from memorymaster.dreaming.ledger import DreamLedger

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
CLAUDE_SESSION = "11111111-2222-3333-4444-555555555555"
CODEX_THREAD = "99999999-8888-7777-6666-555555555555"


def _touch(path: Path, age_seconds: float) -> None:
    stamp = NOW.timestamp() - age_seconds
    os.utime(path, (stamp, stamp))


def _claude_line(text: str, role: str, cwd: Path, uid: str) -> str:
    return json.dumps({
        "type": role, "sessionId": CLAUDE_SESSION, "cwd": str(cwd), "uuid": uid,
        "timestamp": "2026-10-20T11:50:00Z", "message": {"role": role, "content": text},
    }) + "\n"


def _codex_lines(text: str, cwd: Path) -> str:
    meta = {"type": "session_meta", "payload": {"id": CODEX_THREAD, "cwd": str(cwd)}}
    msgs = [
        {"type": "response_item", "timestamp": "2026-10-20T11:50:00Z",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}},
        {"type": "response_item", "timestamp": "2026-10-20T11:50:05Z",
         "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ack " + text}]}},
    ]
    return "\n".join(json.dumps(item) for item in (meta, *msgs)) + "\n"


@pytest.fixture()
def world(tmp_path: Path):
    project = tmp_path / "work" / "alpha"
    (project / ".git").mkdir(parents=True)
    claude_root = tmp_path / "claude-projects"
    codex_root = tmp_path / "codex-sessions"
    (claude_root / "proj-dir").mkdir(parents=True)
    (codex_root / "2026" / "10" / "20").mkdir(parents=True)
    claude_file = claude_root / "proj-dir" / f"{CLAUDE_SESSION}.jsonl"
    claude_file.write_text(
        _claude_line("first question about the build pipeline", "user", project, "u1")
        + _claude_line("first answer about the build pipeline", "assistant", project, "u2"),
        encoding="utf-8",
    )
    codex_file = codex_root / "2026" / "10" / "20" / f"rollout-2026-10-20T11-50-00-{CODEX_THREAD}.jsonl"
    codex_file.write_text(_codex_lines("codex question about deployment", project), encoding="utf-8")
    _touch(claude_file, 120)
    _touch(codex_file, 60)
    return {
        "ledger": DreamLedger(tmp_path / "state" / "capture-control.db"),
        "roots": {"claude": claude_root, "codex": codex_root},
        "claude_file": claude_file, "codex_file": codex_file, "project": project, "tmp": tmp_path,
    }


def _rows(ledger: DreamLedger) -> list[sqlite3.Row]:
    with sqlite3.connect(ledger.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM dream_captures ORDER BY id").fetchall()


def test_scan_captures_recent_claude_and_codex_transcripts_and_records_source_mtime(world) -> None:
    summary = scan_active_transcripts(world["ledger"], roots=world["roots"], now=NOW)

    rows = _rows(world["ledger"])
    assert summary["queued"] == 2
    assert {row["provider"] for row in rows} == {"claude", "codex"}
    assert {row["scope"] for row in rows} == {"project:alpha"}
    by_provider = {row["provider"]: row for row in rows}
    assert by_provider["claude"]["source_mtime"] == pytest.approx(NOW.timestamp() - 120, abs=1)
    assert by_provider["codex"]["source_mtime"] == pytest.approx(NOW.timestamp() - 60, abs=1)


def test_scan_is_incremental_and_resumes_from_the_byte_cursor(world) -> None:
    ledger = world["ledger"]
    scan_active_transcripts(ledger, roots=world["roots"], now=NOW)
    first = _rows(ledger)

    again = scan_active_transcripts(ledger, roots=world["roots"], now=NOW)
    assert again["scanned"] == 0 and len(_rows(ledger)) == len(first)

    with world["claude_file"].open("a", encoding="utf-8") as handle:
        handle.write(_claude_line("second question about caching", "user", world["project"], "u3"))
        handle.write(_claude_line("second answer about caching", "assistant", world["project"], "u4"))
    _touch(world["claude_file"], 10)
    scan_active_transcripts(ledger, roots=world["roots"], now=NOW)

    claude_rows = [row for row in _rows(ledger) if row["provider"] == "claude"]
    # The new bytes coalesce into the still-captured row: one row, cursor moved to the new end.
    assert claude_rows[-1]["cursor_end"] == world["claude_file"].stat().st_size
    assert "caching" in claude_rows[-1]["messages_json"]
    assert "build pipeline" in claude_rows[-1]["messages_json"]


def test_cold_start_ignores_transcripts_older_than_the_lookback(world) -> None:
    _touch(world["claude_file"], 5 * 3600)

    scan_active_transcripts(world["ledger"], roots=world["roots"], now=NOW)

    assert {row["provider"] for row in _rows(world["ledger"])} == {"codex"}


def test_max_files_bounds_one_tick_and_the_rest_is_picked_up_next_tick(world) -> None:
    ledger = world["ledger"]

    first = scan_active_transcripts(ledger, max_files=1, roots=world["roots"], now=NOW)
    assert first["scanned"] == 1
    assert {row["provider"] for row in _rows(ledger)} == {"claude"}  # oldest first

    second = scan_active_transcripts(ledger, max_files=1, roots=world["roots"], now=NOW)
    assert second["scanned"] == 1
    assert {row["provider"] for row in _rows(ledger)} == {"claude", "codex"}


def test_watermark_lives_in_the_ledger_state_db(world) -> None:
    scan_active_transcripts(world["ledger"], roots=world["roots"], now=NOW)

    assert world["ledger"].scan_watermark("scan:claude") == pytest.approx(NOW.timestamp() - 120, abs=1)
    assert world["ledger"].scan_watermark("scan:codex") == pytest.approx(NOW.timestamp() - 60, abs=1)
    # A fresh ledger object on the same file sees it: persisted, not in memory or in the home dir.
    assert DreamLedger(world["ledger"].db_path).scan_watermark("scan:codex") is not None


def test_scan_shares_the_hook_cursor_so_the_same_bytes_are_never_captured_twice(world) -> None:
    ledger = world["ledger"]
    capture_hook_payload(
        {"transcript_path": str(world["claude_file"]), "session_id": CLAUDE_SESSION, "cwd": str(world["project"])},
        provider="claude", ledger=ledger,
    )
    capture_hook_payload(
        {"transcript_path": str(world["codex_file"]), "session_id": CODEX_THREAD, "cwd": str(world["project"])},
        provider="codex", ledger=ledger,
    )
    before = len(_rows(ledger))

    scan_active_transcripts(ledger, roots=world["roots"], now=NOW)

    assert len(_rows(ledger)) == before


def test_scan_skips_subagent_transcripts_and_files_without_a_working_directory(world) -> None:
    sub = world["roots"]["claude"] / "proj-dir" / CLAUDE_SESSION / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-abc.jsonl").write_text(_claude_line("subagent chatter", "user", world["project"], "s1"), encoding="utf-8")
    _touch(sub / "agent-abc.jsonl", 30)
    nocwd = world["roots"]["claude"] / "proj-dir" / "orphan-session.jsonl"
    nocwd.write_text(json.dumps({"type": "user", "sessionId": "orphan-session",
                                 "message": {"role": "user", "content": "no cwd here"}}) + "\n", encoding="utf-8")
    _touch(nocwd, 30)

    summary = scan_active_transcripts(world["ledger"], roots=world["roots"], now=NOW)

    assert summary["skipped_no_cwd"] == 1
    assert all("subagent chatter" not in row["messages_json"] for row in _rows(world["ledger"]))
    assert all("no cwd here" not in row["messages_json"] for row in _rows(world["ledger"]))


def test_missing_roots_are_not_an_error(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "state.db")

    summary = scan_active_transcripts(ledger, roots={"claude": tmp_path / "nope", "codex": tmp_path / "nada"}, now=NOW)

    assert summary["scanned"] == 0 and summary["errors"] == 0


def test_a_failing_capture_does_not_advance_the_watermark_past_it(world, monkeypatch) -> None:
    from memorymaster.dreaming import capture

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(capture, "capture_transcript", boom)
    summary = scan_active_transcripts(world["ledger"], roots=world["roots"], now=NOW)
    assert summary["errors"] >= 1
    monkeypatch.undo()

    scan_active_transcripts(world["ledger"], roots=world["roots"], now=NOW)
    assert {row["provider"] for row in _rows(world["ledger"])} == {"claude", "codex"}


def test_existing_ledger_without_the_column_is_upgraded_on_open(tmp_path: Path) -> None:
    path = tmp_path / "capture-control.db"
    DreamLedger(path)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE dream_captures DROP COLUMN source_mtime")
        conn.execute("DELETE FROM dream_schema_versions WHERE version = 4")

    DreamLedger(path)

    with sqlite3.connect(path) as conn:
        assert "source_mtime" in {row[1] for row in conn.execute("PRAGMA table_info(dream_captures)")}
        assert 4 in {row[0] for row in conn.execute("SELECT version FROM dream_schema_versions")}
