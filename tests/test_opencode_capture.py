"""Disposable OpenCode stores; shapes follow upstream, never a user's store.

https://github.com/anomalyco/opencode/blob/v1.2.0/packages/opencode/src/session/session.sql.ts
https://github.com/anomalyco/opencode/blob/v1.2.0/packages/opencode/src/storage/json-migration.ts
https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/sql.ts
https://github.com/anomalyco/opencode/blob/dev/packages/schema/src/session-message.ts
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memorymaster.dreaming.capture import capture_hook_payload, capture_opencode_session
from memorymaster.dreaming.ledger import DreamLedger


@pytest.fixture(autouse=True)
def _isolated_hook_events(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_HOOK_EVENTS_LOG", str(tmp_path / "hook-events.jsonl"))


def _fixture(tmp_path: Path) -> tuple[Path, dict, DreamLedger]:
    workspace = tmp_path / "fixture-project"
    workspace.mkdir()
    document = json.loads((Path(__file__).parent / "fixtures/opencode/session.json").read_text(encoding="utf-8"))
    document["info"]["directory"] = str(workspace)
    return workspace, document, DreamLedger(tmp_path / "dream.db")


def _store(root: Path, document: dict, layout: str) -> Path:
    root.mkdir(exist_ok=True)
    if layout == "json":
        records = [(root / "storage/session/fixture-project/ses_fixture.json", document["info"])]
        for message in document["messages"]:
            info = message["info"]
            records.append((root / f"storage/message/ses_fixture/{info['id']}.json", info))
            for part in message["parts"]:
                records.append((root / f"storage/part/{info['id']}/{part['id']}.json", {
                    **part, "sessionID": "ses_fixture", "messageID": info["id"],
                }))
        for path, record in records:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record), encoding="utf-8")
        return root
    with sqlite3.connect(root / "opencode.db") as conn:
        table = "session_v2" if layout == "v2" else "session"
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table}(id TEXT PRIMARY KEY, directory TEXT)")
        conn.execute(f"INSERT OR REPLACE INTO {table} VALUES (?, ?)", ("ses_fixture", document["info"]["directory"]))
        if layout == "v2":
            conn.execute("CREATE TABLE IF NOT EXISTS session_message(id TEXT PRIMARY KEY,session_id TEXT,type TEXT,seq INTEGER,time_created INTEGER,data TEXT)")
        else:
            conn.execute("CREATE TABLE IF NOT EXISTS message(id TEXT PRIMARY KEY,session_id TEXT,time_created INTEGER,data TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS part(id TEXT PRIMARY KEY,message_id TEXT,session_id TEXT,data TEXT)")
        for seq, message in enumerate(document["messages"]):
            info = message["info"]
            if layout == "v2":
                data = {"time": info["time"]}
                if info["role"] == "user":
                    data["text"] = "\n".join(part["text"] for part in message["parts"] if part["type"] == "text")
                else:
                    data["content"] = message["parts"]
                conn.execute("INSERT OR REPLACE INTO session_message VALUES (?,?,?,?,?,?)", (
                    info["id"], "ses_fixture", info["role"], seq, info["time"]["created"], json.dumps(data),
                ))
            else:
                conn.execute("INSERT OR REPLACE INTO message VALUES (?,?,?,?)", (
                    info["id"], "ses_fixture", info["time"]["created"], json.dumps(info),
                ))
                for part in message["parts"]:
                    conn.execute("INSERT OR REPLACE INTO part VALUES (?,?,?,?)", (
                        part["id"], info["id"], "ses_fixture", json.dumps(part),
                    ))
    return root


@pytest.mark.parametrize("layout", ["sqlite", "json", "v2"])
def test_capture_real_storage_shapes_and_repeat_idle(tmp_path: Path, layout: str, monkeypatch) -> None:
    workspace, document, ledger = _fixture(tmp_path)
    root = _store(tmp_path / "opencode", document, layout)
    events = []
    monkeypatch.setattr("memorymaster.surfaces.hook_events.append_hook_event", lambda *args: events.append(args))
    first = capture_hook_payload({"session_id": "ses_fixture", "cwd": str(workspace), "data_dir": str(root)}, provider="opencode", ledger=ledger)
    assert first["queued"] == 1
    capture = ledger.get_capture(first["capture_id"])
    assert capture["provider"] == "opencode"
    assert capture["scope"] == "project:fixture-project"
    assert [m["text"] for m in capture["messages"]] == [
        "Prefer deterministic fixtures for capture.", "I will use disposable fixtures.",
    ]
    assert "private reasoning" not in json.dumps(capture)
    assert capture_opencode_session("ses_fixture", cwd=str(workspace), ledger=ledger, data_dir=root) == {"queued": 0, "reason": "no_increment"}
    document["messages"].append({"info": {"id": "msg_003", "role": "user", "time": {"created": 1793000002000}}, "parts": [
        {"id": "prt_004", "type": "text", "text": "Keep the next turn too."},
    ]})
    _store(root, document, layout)
    added = capture_opencode_session("ses_fixture", cwd=str(workspace), ledger=ledger, data_dir=root)
    assert added["messages"] == 1
    assert added["capture_id"] == first["capture_id"]
    assert len(ledger.get_capture(first["capture_id"])["messages"]) == 3
    assert events == [("opencode", "capture", 2), ("opencode", "capture", 0), ("opencode", "capture", 1)]


@pytest.mark.parametrize("layout", ["sqlite", "json", "v2"])
def test_governed_filtering_and_cross_idle_memory_echo(tmp_path: Path, layout: str) -> None:
    workspace, document, ledger = _fixture(tmp_path)
    secret = "sk-LiveSecret1234567890abcd"
    document["messages"][0]["parts"][0]["text"] = f"Use {secret} from C:\\Users\\test\\private.txt"
    document["messages"][1]["parts"] = [{"id": "prt_tool", "type": "tool", "tool": "memorymaster_query_memory", "name": "memorymaster_query_memory", "callID": "call_memory", "state": {"output": "secret recalled evidence"}}]
    root = _store(tmp_path / "opencode", document, layout)
    first = capture_opencode_session("ses_fixture", cwd=str(workspace), ledger=ledger, data_dir=root)
    assert first["messages"] == 1
    document["messages"].extend([
        {"info": {"id": "msg_003", "role": "assistant", "time": {"created": 1793000002000}}, "parts": [{"id": "prt_echo", "type": "text", "text": "Unmarked echo of recalled evidence"}]},
        {"info": {"id": "msg_004", "role": "user", "time": {"created": 1793000003000}}, "parts": [{"id": "prt_injected", "type": "text", "synthetic": True, "text": "Injected context should never become evidence"}]},
        {"info": {"id": "msg_005", "role": "user", "time": {"created": 1793000004000}}, "parts": [{"id": "prt_user", "type": "text", "text": "A new authored request clears contamination."}]},
    ])
    # v2 has an explicit synthetic record type instead of a synthetic text part.
    _store(root, document, layout)
    if layout == "v2":
        with sqlite3.connect(root / "opencode.db") as conn:
            conn.execute("UPDATE session_message SET type='synthetic' WHERE id='msg_004'")
    result = capture_opencode_session("ses_fixture", cwd=str(workspace), ledger=ledger, data_dir=root)
    assert result["messages"] == 1
    persisted = json.dumps(ledger.get_capture(first["capture_id"]))
    for excluded in (secret, "C:\\\\Users\\\\test", "secret recalled evidence", "Unmarked echo", "Injected context"):
        assert excluded not in persisted
    assert "A new authored request" in persisted


@pytest.mark.parametrize("layout", ["sqlite", "json", "v2"])
def test_session_cannot_be_captured_under_another_workspace(tmp_path: Path, layout: str) -> None:
    workspace, document, ledger = _fixture(tmp_path)
    root = _store(tmp_path / "opencode", document, layout)
    other = tmp_path / "other-project"
    other.mkdir()
    result = capture_opencode_session("ses_fixture", cwd=str(other), ledger=ledger, data_dir=root)
    assert result == {"queued": 0, "reason": "workspace_mismatch"}
    assert ledger.status()["queue"].get("captured", 0) == 0
    assert capture_opencode_session("ses_fixture", cwd=str(workspace), ledger=ledger, data_dir=root)["queued"] == 1


def test_missing_and_malformed_storage_fail_closed(tmp_path: Path) -> None:
    workspace, document, ledger = _fixture(tmp_path)
    root = tmp_path / "missing"
    assert capture_opencode_session("ses_fixture", cwd=str(workspace), ledger=ledger, data_dir=root)["queued"] == 0
    assert not root.exists()
    for session_id in ("../ses_fixture", "", "ses_*"):
        assert capture_opencode_session(session_id, cwd=str(workspace), ledger=ledger, data_dir=root)["reason"] == "invalid_session"
    root.mkdir()
    (root / "opencode.db").write_text("invalid database", encoding="utf-8")
    assert capture_opencode_session("ses_fixture", cwd=str(workspace), ledger=ledger, data_dir=root)["reason"] == "storage_unavailable"


@pytest.mark.parametrize("layout", ["sqlite", "json", "v2"])
def test_failed_enqueue_does_not_advance_capture_cursor(tmp_path: Path, layout: str, monkeypatch) -> None:
    workspace, document, ledger = _fixture(tmp_path)
    root = _store(tmp_path / "opencode", document, layout)
    enqueue = ledger.enqueue

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("disposable failure")

    monkeypatch.setattr(ledger, "enqueue", fail)
    with pytest.raises(sqlite3.OperationalError, match="disposable failure"):
        capture_opencode_session("ses_fixture", cwd=str(workspace), ledger=ledger, data_dir=root)
    monkeypatch.setattr(ledger, "enqueue", enqueue)
    assert capture_opencode_session("ses_fixture", cwd=str(workspace), ledger=ledger, data_dir=root)["messages"] == 2


def test_sqlite_capture_uses_readonly_snapshot_and_does_not_create_missing_session(tmp_path: Path, monkeypatch) -> None:
    workspace, document, ledger = _fixture(tmp_path)
    root = _store(tmp_path / "opencode", document, "sqlite")
    original = (root / "opencode.db").read_bytes()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    assert capture_opencode_session("ses_fixture", cwd=str(workspace), ledger=ledger)["queued"] == 1
    assert (root / "opencode.db").read_bytes() == original
    assert capture_opencode_session("ses_missing", cwd=str(workspace), ledger=ledger)["reason"] == "session_not_found"


@pytest.mark.parametrize("layout", ["sqlite", "json", "v2"])
def test_malformed_json_never_advances_cursor(tmp_path: Path, layout: str) -> None:
    workspace, document, ledger = _fixture(tmp_path)
    root = _store(tmp_path / "opencode", document, layout)
    if layout == "json":
        path = root / "storage/part/msg_001/prt_001.json"
        path.write_text("{broken", encoding="utf-8")
    else:
        table = "session_message" if layout == "v2" else "part"
        with sqlite3.connect(root / "opencode.db") as conn:
            conn.execute(f"UPDATE {table} SET data='{{broken'")
    assert capture_opencode_session("ses_fixture", cwd=str(workspace), ledger=ledger, data_dir=root)["reason"] == "storage_unavailable"
    _store(root, document, layout)
    assert capture_opencode_session("ses_fixture", cwd=str(workspace), ledger=ledger, data_dir=root)["messages"] == 2


@pytest.mark.parametrize("layout", ["sqlite", "json"])
def test_authored_user_summary_metadata_is_distinct_from_assistant_compaction(tmp_path: Path, layout: str) -> None:
    # Upstream MessageV2.User.summary is UI metadata; Assistant.summary=true
    # marks generated compaction. These must not be conflated.
    # https://github.com/anomalyco/opencode/blob/v1.2.0/packages/opencode/src/session/message-v2.ts
    workspace, document, ledger = _fixture(tmp_path)
    document["messages"][0]["info"]["summary"] = {"title": "Generated title", "diffs": []}
    document["messages"][1]["info"]["summary"] = True
    root = _store(tmp_path / "opencode", document, layout)
    result = capture_opencode_session("ses_fixture", cwd=str(workspace), ledger=ledger, data_dir=root)
    assert result["messages"] == 1
    assert ledger.get_capture(result["capture_id"])["messages"][0]["text"] == "Prefer deterministic fixtures for capture."
