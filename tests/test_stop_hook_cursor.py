"""T-2274: the Stop-hook distillation cursor.

A provider timeout/outage must not advance the cursor (the turns are retried from the old offset), a retry or a
crash between "applied" and "cursor committed" must not store the learning twice, and a provider that answered
"nothing worth keeping" must be recorded differently from one that failed.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
from pathlib import Path

import pytest

from memorymaster.core import llm_provider
from memorymaster.core.capture_control import CaptureLedger
from memorymaster.core.service import MemoryService

TEMPLATE = Path(__file__).resolve().parents[1] / "memorymaster" / "config_templates" / "hooks" / "memorymaster-auto-ingest.py"
LEARNING = json.dumps([{
    "text": "The steward cycle truncates the WAL before taking snapshots",
    "claim_type": "fact", "subject": "steward", "predicate": "behavior",
}])


class World:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.project = tmp_path / "project"
        self.project.mkdir()
        self.db = self.project / "memorymaster.db"
        MemoryService(self.db, workspace_root=self.project).init_db()
        self.capture_db = tmp_path / "capture.db"
        self.transcript = tmp_path / "session.jsonl"
        self.transcript.write_text(json.dumps({"message": {"role": "assistant", "content": [
            {"type": "text", "text": "Root cause: the steward truncates the WAL before every snapshot is taken."}]}}) + "\n",
            encoding="utf-8")
        for name in ("HOME", "USERPROFILE"):
            monkeypatch.setenv(name, str(tmp_path / "home"))
        for name in ("MEMORYMASTER_STOP_BLOCKING", "MEMORYMASTER_STOP_CAPTURE_VERBATIM", "MEMORYMASTER_STOP_RULE_MINING",
                     "MEMORYMASTER_WAL_DISCIPLINE", "MEMORYMASTER_LLM_FALLBACK_PROVIDER", "MEMORYMASTER_JEV_MODE",
                     "MEMORYMASTER_JEV_RECALL", "MEMORYMASTER_JEV_SESSION"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("MEMORYMASTER_CAPTURE_STATE_DB", str(self.capture_db))
        monkeypatch.setenv("MEMORYMASTER_CAPTURE_SESSION_DAILY_CALLS", "50")
        monkeypatch.setenv("MEMORYMASTER_STOP_EXTRACT", "1")
        monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "ollama")
        self.calls = 0
        self.response = ""
        self.sent: list[str] = []
        self.tmp_path = tmp_path
        monkeypatch.setattr(llm_provider, "_http_post", self._post)
        rendered = tmp_path / "hook.py"
        rendered.write_text(TEMPLATE.read_text(encoding="utf-8").replace(
            "__MEMORYMASTER_PROJECT_ROOT__", self.project.as_posix()), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("stop_hook_cursor_mod", rendered)
        self.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hook)

    def _post(self, url, payload, *args, **kwargs) -> str:
        self.calls += 1
        self.sent.append(str(payload.get("prompt", "")))
        return self.response

    def say(self, text: str) -> None:
        self.transcript.write_text(json.dumps({"message": {"role": "assistant", "content": [
            {"type": "text", "text": text}]}}) + "\n", encoding="utf-8")

    def spool_mode(self) -> None:
        self.monkeypatch.setenv("MEMORYMASTER_WAL_DISCIPLINE", "1")
        self.monkeypatch.setenv("MEMORYMASTER_SPOOL_DIR", str(self.tmp_path / "spool"))

    def hook_log(self) -> str:
        path = self.tmp_path / "home" / ".memorymaster" / "hook_state" / "hook.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def stop(self) -> None:
        self.monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
            "session_id": "cursor-s", "transcript_path": str(self.transcript), "cwd": str(self.project)})))
        self.monkeypatch.setattr("sys.stdout", io.StringIO())
        self.hook.main()

    def outcomes(self) -> list[str]:
        with sqlite3.connect(self.capture_db) as conn:
            return [r[0] for r in conn.execute("SELECT outcome FROM capture_usage ORDER BY created_at, rowid")]

    def pending(self) -> str:
        return CaptureLedger(self.capture_db).read_increment(self.transcript, "cursor-s:stop-distill").text

    def claims(self) -> int:
        with sqlite3.connect(self.db) as conn:
            return conn.execute("SELECT COUNT(*) FROM claims WHERE source_agent='llm-stop-hook'").fetchone()[0]


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


def test_a_provider_timeout_keeps_the_cursor_and_the_retry_applies_once(world: World) -> None:
    world.response = ""  # _http_post returns "" on a timeout / transport error
    world.stop()
    assert world.outcomes() == ["error"]
    assert world.pending(), "the failed turn must still be pending: the cursor did not move"
    assert world.claims() == 0

    world.response = LEARNING
    world.stop()
    assert world.outcomes() == ["error", "ok"]
    assert world.claims() == 1
    assert world.pending() == "", "the successful retry commits the cursor"

    world.stop()  # nothing new in the transcript: no provider call, nothing repeated
    assert world.calls == 2
    assert world.claims() == 1


def test_an_empty_success_is_recorded_apart_from_a_failure_and_advances_the_cursor(world: World) -> None:
    world.response = "[]"
    world.stop()
    assert world.outcomes() == ["empty"]
    assert world.pending() == ""
    assert world.claims() == 0


def test_an_unparseable_answer_is_a_failure_not_an_empty_success(world: World) -> None:
    world.response = "Sorry, I cannot help with that."
    world.stop()
    assert world.outcomes() == ["error"]
    assert world.pending()


def test_a_crash_between_apply_and_cursor_commit_does_not_store_the_learning_twice(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.response = LEARNING
    real_commit = CaptureLedger.commit_cursor

    def crash(self, chunk):
        raise OSError("killed before the cursor moved")

    monkeypatch.setattr(CaptureLedger, "commit_cursor", crash)
    world.stop()
    assert world.claims() == 1 and world.pending()

    monkeypatch.setattr(CaptureLedger, "commit_cursor", real_commit)
    world.stop()  # the same chunk is read again and produces the same learning
    assert world.claims() == 1
    assert world.pending() == ""


def test_a_missing_claims_db_defers_instead_of_dropping_the_turns(world: World) -> None:
    world.response = LEARNING
    world.db.unlink()
    world.stop()
    assert world.outcomes() == ["error"]
    assert world.pending()


def test_the_provider_call_is_an_export_private_ips_and_personal_paths_do_not_leave(world: World) -> None:
    world.say("Root cause: the deploy script on 192.168.10.7 reads C:\\Users\\someone\\proj\\run.py and "
              "/home/someone/proj/run.py before the steward truncates the WAL.")
    world.response = "[]"
    world.stop()
    body = "\n".join(world.sent)
    assert world.calls == 1 and "steward truncates the WAL" in body
    for leaked in ("192.168.10.7", "someone"):
        assert leaked not in body, leaked


def test_a_spool_append_failure_keeps_the_cursor_instead_of_reporting_empty(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memorymaster.core import spool

    world.spool_mode()
    world.response = LEARNING

    def broken(*args, **kwargs):
        raise OSError("spool volume is read-only")

    monkeypatch.setattr(spool, "append", broken)
    world.stop()
    assert world.outcomes() == ["error"]
    assert world.pending()


def test_the_failure_cause_in_hook_log_is_redacted(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.response = LEARNING
    leak = "C:\\Users\\someone\\secret-dir and password=" + "hunter2hunter2"

    def crash(self, chunk):
        raise OSError(leak)

    monkeypatch.setattr(CaptureLedger, "commit_cursor", crash)
    world.stop()
    log = world.hook_log()
    assert "stop-distill failed, cursor kept: OSError" in log
    assert "someone" not in log and "hunter2hunter2" not in log
