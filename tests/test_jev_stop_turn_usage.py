"""Stop hook: the Jev usage joiner runs locally after approve and never changes the output."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.decisions.ledger import DecisionLedger, DecisionRecord, ItemRecord
from memorymaster.recall import jev_surfaces as jev

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "memorymaster" / "config_templates" / "hooks" / "memorymaster-auto-ingest.py"
SESSION = "7f1c0d2e-stop-hook-session"
CLAIM_TEXT = "The prompt index warms zebracache fingerprints before any governed recall injection happens"
APPROVE = json.dumps({"decision": "approve"})


def _project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    service = MemoryService(project / "memorymaster.db", workspace_root=project)
    service.init_db()
    claim = service.ingest(CLAIM_TEXT, [CitationInput(source="test://stop", locator="fixture")], scope="project:p")
    transition_claim(service.store, claim.id, "confirmed", "test fixture")
    other = service.ingest("An unrelated fixture about invoice rounding in the billing module today",
                           [CitationInput(source="test://stop", locator="other")], scope="project:p")
    transition_claim(service.store, other.id, "confirmed", "test fixture")
    hook = tmp_path / "stop-hook.py"
    hook.write_text(TEMPLATE.read_text(encoding="utf-8").replace(
        "__MEMORYMASTER_PROJECT_ROOT__", str(project).replace("\\", "/")), encoding="utf-8")
    return project, hook, claim.id, other.id


def _ledger_with_exposure(path: Path, claim_ids, decided_at: str) -> None:
    ledger = DecisionLedger(path)
    record = DecisionRecord(decision_id="dec-recall-1", ts=decided_at, surface="recall", mode="live",
                            session_key=jev.decision_session_key(SESSION), transport_outcome="ok",
                            attempt_count=1, cost_usd=0.0)
    items = [ItemRecord("dec-recall-1", f"claim:{cid}", "claim", "", exposed=1, delivered=1) for cid in claim_ids]
    assert ledger.write_decision(record, items)


def _transcript(path: Path, *, answer: str, prompt_ts: str, answer_ts: str) -> Path:
    entries = [
        {"type": "user", "uuid": "u-old", "timestamp": "2026-09-23T09:00:00.000Z",
         "message": {"role": "user", "content": "an older prompt that is not part of the last turn"}},
        {"type": "assistant", "timestamp": "2026-09-23T09:00:05.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "older answer"}]}},
        {"type": "user", "uuid": "u-last", "timestamp": prompt_ts,
         "message": {"role": "user", "content": "how does the prompt index warm up?"}},
        {"type": "assistant", "timestamp": prompt_ts.replace(":10.", ":12."),
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "Grep", "input": {"pattern": "billing_rounding_marker"}}]}},
        {"type": "user", "timestamp": prompt_ts.replace(":10.", ":13."),
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "no matches"}]}},
        {"type": "assistant", "timestamp": answer_ts,
         "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]}},
    ]
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
    return path


def _env(tmp_path, **extra):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("MEMORYMASTER_", "TYPESAFE_"))}
    env.update({
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "PYTHONPATH": str(ROOT),
        "MEMORYMASTER_DECISIONS_DB": str(tmp_path / "decisions.db"),
        "MEMORYMASTER_SPOOL_DIR": str(tmp_path / "spool"),
    })
    env.update(extra)
    return env


def _run_hook(hook, payload, env):
    return subprocess.run([sys.executable, str(hook)], input=json.dumps(payload), capture_output=True,
                          text=True, env=env, timeout=60)


def test_live_mode_records_turn_usage_with_every_stop_flag_off(tmp_path):
    project, hook, used_id, unused_id = _project(tmp_path)
    _ledger_with_exposure(tmp_path / "decisions.db", [used_id, unused_id], "2026-09-23T10:00:10.500000+00:00")
    transcript = _transcript(tmp_path / "t.jsonl",
                             answer=f"Answer: {CLAIM_TEXT.lower()}, so the hook stays fast.",
                             prompt_ts="2026-09-23T10:00:10.000Z", answer_ts="2026-09-23T10:00:20.250Z")
    payload = {"session_id": SESSION, "transcript_path": str(transcript), "cwd": str(project),
               "stop_hook_active": False}

    result = _run_hook(hook, payload, _env(tmp_path, MEMORYMASTER_JEV_MODE="live"))

    assert result.returncode == 0
    assert result.stdout == APPROVE
    rows = DecisionLedger(tmp_path / "decisions.db").query("SELECT * FROM outcomes")
    assert [(row["item_ref"], row["kind"], row["label_source"]) for row in rows] == [
        (f"claim:{used_id}", "used_in_turn", "detector")]
    assert rows[0]["observed_at"] == "2026-09-23T10:00:20.250Z"
    assert json.loads(rows[0]["details_json"])["turn_id"] == "u-last"

    replay = _run_hook(hook, payload, _env(tmp_path, MEMORYMASTER_JEV_MODE="live"))
    assert replay.stdout == APPROVE
    assert len(DecisionLedger(tmp_path / "decisions.db").query("SELECT * FROM outcomes")) == 1


def test_tool_inputs_of_the_last_turn_count_as_usage(tmp_path):
    project, hook, used_id, unused_id = _project(tmp_path)
    _ledger_with_exposure(tmp_path / "decisions.db", [used_id, unused_id], "2026-09-23T10:00:10.500000+00:00")
    service = MemoryService(project / "memorymaster.db", workspace_root=project, read_only=True)
    human_id = service.store.get_claim(unused_id).human_id
    assert human_id
    transcript = _transcript(tmp_path / "t.jsonl", answer=f"Checked {human_id} as well.",
                             prompt_ts="2026-09-23T10:00:10.000Z", answer_ts="2026-09-23T10:00:30.000Z")
    payload = {"session_id": SESSION, "transcript_path": str(transcript), "cwd": str(project)}

    result = _run_hook(hook, payload, _env(tmp_path, MEMORYMASTER_JEV_RECALL="shadow"))

    assert result.stdout == APPROVE
    rows = DecisionLedger(tmp_path / "decisions.db").query("SELECT item_ref FROM outcomes")
    assert [row["item_ref"] for row in rows] == [f"claim:{unused_id}"]


def test_off_mode_is_untouched_and_creates_no_ledger(tmp_path):
    project, hook, _, _ = _project(tmp_path)
    transcript = _transcript(tmp_path / "t.jsonl", answer=CLAIM_TEXT, prompt_ts="2026-09-23T10:00:10.000Z",
                             answer_ts="2026-09-23T10:00:20.000Z")
    result = _run_hook(hook, {"session_id": SESSION, "transcript_path": str(transcript), "cwd": str(project)},
                       _env(tmp_path))
    assert result.stdout == APPROVE
    assert not (tmp_path / "decisions.db").exists()


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("jev_stop_hook", TEMPLATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_slow_joiner_is_bounded_and_output_stays_exact(tmp_path, monkeypatch, capsys):
    for name in list(os.environ):
        if name.startswith(("MEMORYMASTER_", "TYPESAFE_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMORYMASTER_JEV_MODE", "live")
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(tmp_path / "decisions.db"))
    release = threading.Event()
    calls = []

    def slow(data, **kwargs):
        calls.append(data.get("session_id"))
        release.wait(5)
        return 0

    monkeypatch.setattr(jev, "record_stop_turn_usage", slow)
    module = _load_hook_module()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": SESSION, "transcript_path": ""})))

    started = time.perf_counter()
    module.main()
    elapsed = time.perf_counter() - started
    release.set()

    assert calls == [SESSION]
    assert elapsed < 1.0
    assert capsys.readouterr().out == APPROVE


def test_joiner_errors_never_change_the_decision(tmp_path, monkeypatch, capsys):
    for name in list(os.environ):
        if name.startswith(("MEMORYMASTER_", "TYPESAFE_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMORYMASTER_JEV_MODE", "shadow")
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(tmp_path / "decisions.db"))

    def boom(*_a, **_k):
        raise RuntimeError("joiner failure")

    monkeypatch.setattr(jev, "record_stop_turn_usage", boom)
    module = _load_hook_module()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": SESSION, "stop_hook_active": True})))
    module.main()
    assert capsys.readouterr().out == APPROVE


def test_read_last_turn_takes_the_last_prompt_onwards(tmp_path):
    transcript = _transcript(tmp_path / "t.jsonl", answer="final words", prompt_ts="2026-09-23T10:00:10.000Z",
                             answer_ts="2026-09-23T10:00:20.000Z")
    turn = jev.read_last_turn(transcript)
    assert turn["turn_id"] == "u-last" and turn["observed_at"] == "2026-09-23T10:00:20.000Z"
    assert "final words" in turn["assistant_text"] and "older answer" not in turn["assistant_text"]
    assert any("billing_rounding_marker" in value for value in turn["tool_inputs"])
    assert jev.read_last_turn(tmp_path / "missing.jsonl") is None


def test_read_last_turn_is_bounded_to_the_file_tail(tmp_path):
    transcript = tmp_path / "big.jsonl"
    filler = json.dumps({"type": "assistant", "timestamp": "2026-09-23T08:00:00.000Z",
                         "message": {"role": "assistant", "content": [{"type": "text", "text": "x" * 1000}]}})
    last = json.dumps({"type": "assistant", "timestamp": "2026-09-23T11:00:00.000Z",
                       "message": {"role": "assistant", "content": [{"type": "text", "text": "tail answer"}]}})
    transcript.write_text("\n".join([filler] * 2000 + [last]) + "\n", encoding="utf-8")
    turn = jev.read_last_turn(transcript, tail_bytes=64 * 1024)
    assert turn["observed_at"] == "2026-09-23T11:00:00.000Z"
    assert len(turn["assistant_text"]) <= 70 * 1024


@pytest.mark.parametrize("mode_env", [{}, {"MEMORYMASTER_JEV_MODE": "off"}, {"MEMORYMASTER_JEV_MODE": "banana"}])
def test_joiner_disabled_unless_recall_or_session_mode_is_on(tmp_path, monkeypatch, mode_env):
    for name in list(os.environ):
        if name.startswith(("MEMORYMASTER_JEV", "MEMORYMASTER_DECISIONS")):
            monkeypatch.delenv(name, raising=False)
    for key, value in mode_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(tmp_path / "decisions.db"))
    assert jev.turn_usage_enabled() is False
    assert jev.record_stop_turn_usage({"session_id": SESSION}, db_path=tmp_path / "none.db") == 0
    assert not (tmp_path / "decisions.db").exists()
    monkeypatch.setenv("MEMORYMASTER_JEV_SESSION", "live")
    assert jev.turn_usage_enabled() is True


def test_read_last_turn_ignores_meta_user_entries(tmp_path):
    transcript = _transcript(tmp_path / "t.jsonl", answer="final words", prompt_ts="2026-09-23T10:00:10.000Z",
                             answer_ts="2026-09-23T10:00:20.000Z")
    extra = [  # e.g. a skill body Claude Code injects mid-turn: not a human prompt
        {"type": "user", "isMeta": True, "uuid": "u-meta", "timestamp": "2026-09-23T10:00:21.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "Base directory for this skill: x"}]}},
        {"type": "assistant", "timestamp": "2026-09-23T10:00:25.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "after the skill loaded"}]}},
    ]
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(json.dumps(entry) for entry in extra) + "\n")

    turn = jev.read_last_turn(transcript)

    assert turn["turn_id"] == "u-last" and turn["observed_at"] == "2026-09-23T10:00:25.000Z"
    assert "final words" in turn["assistant_text"] and "after the skill loaded" in turn["assistant_text"]
    assert any("billing_rounding_marker" in value for value in turn["tool_inputs"])
