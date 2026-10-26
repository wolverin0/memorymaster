"""Replay source binding against disposable SQLite, including the read-only boundary."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from memorymaster.dreaming.models import DreamCandidate
from memorymaster.dreaming.source_review import MAX_SOURCE_CHARS, bind_source
from scripts import replay_source_binding as replay_script


@pytest.fixture
def replay_database(tmp_path):
    database = tmp_path / "capture # fixture.db"
    now = datetime.now(timezone.utc)
    candidate = DreamCandidate("dc-test", "The importer limit is 30 days.", "fact",
                               "importer", "limit", "30 days", "project", "m1", "30 days", .8)
    messages = [{"message_id": "m1", "role": "assistant", "text": "The limit is 30 days.", "timestamp": ""},
                {"message_id": "m2", "role": "user", "text": "Actually use 60 days.", "timestamp": ""}]
    old = {"message_id": "old", "role": "user", "text": "x" * MAX_SOURCE_CHARS, "timestamp": ""}
    decisions = [{"candidate_id": candidate.candidate_id, "action": "ignore"}]
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE dream_captures (id INTEGER PRIMARY KEY, scope TEXT, provider TEXT, "
                     "session_hash TEXT, messages_json TEXT, extraction_json TEXT, decisions_json TEXT, "
                     "state TEXT, captured_at TEXT)")

        def insert(source, candidates, *, age=0, state="applied", reviewed=decisions):
            conn.execute("INSERT INTO dream_captures VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?)", (
                "project:test", "codex", "session", json.dumps(source), json.dumps(candidates),
                json.dumps(reviewed) if reviewed is not None else None,
                state, (now - timedelta(days=age)).isoformat()))

        insert(messages, [candidate.to_dict()], age=2)
        # An extraction annotation must not break candidate reconstruction.
        insert([old, *messages], [{**candidate.to_dict(), "jev_ingest": {"status": "admitted"}},
                                 replace(candidate, candidate_id="dc-old", evidence_message_id="old").to_dict()])
        insert([*messages, old], [candidate.to_dict()])  # Later correction cannot fit.
        for options in ({"age": 31}, {"age": -1}, {"state": "consolidated"},
                        {"reviewed": []}, {"reviewed": None}):
            insert(messages, [candidate.to_dict()], **options)
    return database, now, candidate, messages


def test_replay_counts_candidates_and_enforces_read_only(replay_database, monkeypatch):
    database, now, candidate, messages = replay_database
    original = database.read_bytes()
    connect = sqlite3.connect
    openings = []

    def read_only_connect(database_uri, **kwargs):
        openings.append((database_uri, kwargs))
        conn = connect(database_uri, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE dream_captures SET state='captured'")
        conn.rollback()
        return conn

    monkeypatch.setattr(replay_script.sqlite3, "connect", read_only_connect)
    assert replay_script.replay(database, now=now) == {
        "total": 4, "complete_before": 1, "complete_after": 2, "flipped_to_complete": 1}
    assert replay_script.replay(database, days=1, now=now) == {
        "total": 3, "complete_before": 0, "complete_after": 1, "flipped_to_complete": 1}
    assert all(uri.endswith("?mode=ro") and kwargs == {"uri": True} for uri, kwargs in openings)
    assert database.read_bytes() == original
    row = {"scope": "project:test", "provider": "codex", "session_hash": "session", "messages": messages}
    assert replay_script._bind_source_before(candidate, row).to_dict() == bind_source(candidate, row).to_dict()


def test_replay_cli_prints_only_counts(replay_database, capsys):
    database, _, _, _ = replay_database
    assert replay_script.main([str(database)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "total": 4, "complete_before": 1, "complete_after": 2, "flipped_to_complete": 1}
    assert replay_script.main([str(database), "--days", "1"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "total": 3, "complete_before": 0, "complete_after": 1, "flipped_to_complete": 1}


def test_replay_missing_database_is_not_created(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        replay_script.replay(missing)
    assert not missing.exists()


def test_replay_rejects_nonpositive_days(tmp_path):
    with pytest.raises(ValueError, match="positive"):
        replay_script.replay(tmp_path / "missing.db", days=0)
    with pytest.raises(SystemExit) as error:
        replay_script.main([str(tmp_path / "missing.db"), "--days", "-1"])
    assert error.value.code == 2
