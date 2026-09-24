"""Read-only S1 archive gate over the decisions ledger (F-20)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from memorymaster.decisions import archive_gate


def _ledger(path: Path, rows: list[tuple[str, str, str, str, str | None, str]]) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE decisions(decision_id TEXT PRIMARY KEY, ts TEXT, surface TEXT, mode TEXT,"
        " fallback_reason TEXT, action_taken TEXT);"
        "CREATE TABLE decision_items(decision_id TEXT, item_ref TEXT, question_id TEXT);"
    )
    for decision_id, ts, surface, mode, fallback, action in rows:
        conn.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?)",
                     (decision_id, ts, surface, mode, fallback, action))
    conn.commit()
    conn.close()
    return path


def _item(path: Path, decision_id: str, item_ref: str) -> None:
    with sqlite3.connect(path) as conn:
        for question in ("lifecycle.still_valid", "lifecycle.useful_future"):
            conn.execute("INSERT INTO decision_items VALUES (?,?,?)", (decision_id, item_ref, question))


def test_default_path_and_env_override(monkeypatch, tmp_path):
    monkeypatch.delenv("MEMORYMASTER_DECISIONS_DB", raising=False)
    assert archive_gate.ledger_path().parts[-2:] == (".memorymaster", "decisions.db")
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(tmp_path / "x.db"))
    assert archive_gate.ledger_path() == tmp_path / "x.db"


def test_missing_ledger_is_false_and_never_created(monkeypatch, tmp_path):
    ledger = tmp_path / "absent.db"
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(ledger))
    assert archive_gate.has_no_longer_useful_judgment(7) is False
    assert not ledger.exists()


def test_ledger_without_tables_is_false(tmp_path):
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert archive_gate.has_no_longer_useful_judgment(7, db_path=empty) is False


def test_live_judgment_true_and_ledger_unchanged(tmp_path):
    ledger = _ledger(tmp_path / "Py Apps ledger.db", [
        ("d1", "2026-09-20T10:00:00+00:00", "REVALIDATE", "live", None, "no_longer_useful"),
    ])
    _item(ledger, "d1", "7")
    before = ledger.read_bytes()

    assert archive_gate.has_no_longer_useful_judgment(7, db_path=ledger) is True
    assert archive_gate.has_no_longer_useful_judgment(8, db_path=ledger) is False
    assert ledger.read_bytes() == before, "the gate is read-only"


def test_not_before_and_latest_verdict(tmp_path):
    ledger = _ledger(tmp_path / "ledger.db", [
        ("d1", "2026-09-20T10:00:00Z", "REVALIDATE", "live", "", "no_longer_useful"),
        ("d2", "2026-09-21 10:00:00", "REVALIDATE", "live", None, "keep_stale"),
        ("d3", "2026-09-22T10:00:00+00:00", "REVALIDATE", "live", None, "no_longer_useful"),
    ])
    _item(ledger, "d1", "7")
    _item(ledger, "d2", "7")
    _item(ledger, "d3", "claim:9")

    assert archive_gate.judged_no_longer_useful([7, 9], db_path=ledger) == {9}
    assert archive_gate.has_no_longer_useful_judgment(
        9, not_before="2026-09-23T00:00:00+00:00", db_path=ledger) is False
    assert archive_gate.has_no_longer_useful_judgment(
        9, not_before="2026-09-21T00:00:00+00:00", db_path=ledger) is True


def _engine_json(value: object) -> str:
    """``decisions.engine._dumps``: how the engine stores ``action_taken``."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def test_engine_shaped_judgment_counts(tmp_path):
    """Review B1: the engine lowercases the surface and JSON-encodes the action.

    ``engine.decide`` stores ``surface='revalidate'`` and
    ``action_taken='"no_longer_useful"'``; the gate must see that as a judgment,
    also while another process holds the WAL ledger open with the row
    still in the ``-wal`` file.
    """
    ledger = tmp_path / "decisions.db"
    writer = sqlite3.connect(ledger)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.executescript(
        "CREATE TABLE decisions(decision_id TEXT PRIMARY KEY, ts TEXT, surface TEXT, mode TEXT,"
        " fallback_reason TEXT, action_taken TEXT);"
        "CREATE TABLE decision_items(decision_id TEXT, item_ref TEXT, question_id TEXT);"
    )
    writer.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?)",
                   ("dec-1", "2026-09-20T10:00:00.000000+00:00", "revalidate", "live", None,
                    _engine_json("no_longer_useful")))
    for question in ("lifecycle.still_valid", "lifecycle.durable", "lifecycle.useful_future"):
        writer.execute("INSERT INTO decision_items VALUES (?,?,?)", ("dec-1", "claim:42", question))
    writer.commit()
    try:
        assert Path(f"{ledger}-wal").exists(), "the row must still be in the WAL for this case"
        assert archive_gate.has_no_longer_useful_judgment(42, db_path=ledger) is True
        assert archive_gate.judged_no_longer_useful([42, 43], db_path=ledger) == {42}
    finally:
        writer.close()


def test_engine_shaped_later_verdict_revokes(tmp_path):
    ledger = _ledger(tmp_path / "ledger.db", [
        ("d1", "2026-09-20T10:00:00+00:00", "revalidate", "live", None, _engine_json("no_longer_useful")),
        ("d2", "2026-09-21T10:00:00+00:00", "revalidate", "live", None, _engine_json("keep_stale")),
        ("d3", "2026-09-20T10:00:00+00:00", "revalidate", "live", None, _engine_json({"verdict": "x"})),
        ("d4", "2026-09-20T10:00:00+00:00", "revalidate", "shadow", None, _engine_json("no_longer_useful")),
    ])
    _item(ledger, "d1", "7")
    _item(ledger, "d2", "7")
    _item(ledger, "d3", "8")
    _item(ledger, "d4", "9")

    assert archive_gate.judged_no_longer_useful([7, 8, 9], db_path=ledger) == set()
