"""T-1633 (AO-1): the PreCompact residual survives a compaction.

Hermetic: a fake local provider, tmp homes, tmp databases. Never the authoritative database.
"""
from __future__ import annotations

import importlib.util
import io
import json
import runpy
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from memorymaster.capture import precompact_residual as pr
from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.promotion_policy import PromotionPolicyBlocked
from memorymaster.core.service import MemoryService
from memorymaster.recall import residual as state

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "memorymaster" / "config_templates" / "hooks"


def _probe_module():
    spec = importlib.util.spec_from_file_location("probe_m2_continuity", ROOT / "scripts" / "probe_m2_continuity.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _probe_module()


# --- bounded residual --------------------------------------------------------

def test_bound_residual_caps_item_count_and_size():
    raw = {
        "decisions": [f"decision number {i} " + "x" * 400 for i in range(30)],
        "rejected": [{"option": f"opt {i}", "why": "because " * 40} for i in range(30)],
        "assumptions": [f"assumption {i}" for i in range(30)],
        "cards": [f"T-{1000 + i}" for i in range(30)],
        "next_step": "n" * 900,
    }
    items = state.bound_residual(raw)
    assert 0 < len(items) <= state.MAX_ITEMS
    assert len(state.render_block(items)) <= state.MAX_CHARS
    assert all(len(item["text"]) <= state.MAX_ITEM_CHARS for item in items)


def test_bound_residual_keeps_priority_order_when_trimming():
    items = state.bound_residual(
        {"decisions": [f"d{i}" for i in range(10)], "cards": ["T-1234"], "next_step": "go"}
    )
    assert [item["kind"] for item in items].count("decision") == 10
    assert not any(item["kind"] in ("card", "next_step") for item in items)  # lowest priority dropped first


def test_bound_residual_validates_card_ids_dedupes_and_ignores_garbage():
    items = state.bound_residual(
        {"decisions": ["same", "Same", ""], "cards": ["T-1633", "t-1633", "rm -rf", "T-1"], "unknown": ["x"], "next_step": ""}
    )
    assert [i["text"] for i in items if i["kind"] == "decision"] == ["same"]
    assert [i["text"] for i in items if i["kind"] == "card"] == ["T-1633"]
    assert state.bound_residual("not a dict") == []
    assert state.bound_residual([]) == []
    assert state.render_block([]) == ""


def test_residual_state_round_trip_ttl_and_consume(tmp_path):
    items = state.bound_residual({"decisions": ["keep WAL on"]})
    assert state.save_residual("sess/../1", [], directory=tmp_path) is None  # nothing to keep, no marker
    path = state.save_residual("sess/../1", items, directory=tmp_path, now=1000.0)
    assert path == tmp_path / "residual_sess1.json"  # traversal characters stripped
    assert "keep WAL on" in state.pending_block("sess/../1", directory=tmp_path, now=1000.0 + 60)
    assert state.pending_block("sess/../1", directory=tmp_path, now=1000.0 + state.TTL_SECONDS + 1) == ""
    state.consume("sess/../1", directory=tmp_path)
    assert not path.exists()
    assert state.pending_block("sess/../1", directory=tmp_path) == ""


def test_state_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv(state.ENV_STATE_DIR, str(tmp_path / "custom"))
    assert state.residual_path("abc") == tmp_path / "custom" / "residual_abc.json"


# --- transcript slice and extraction ----------------------------------------

def test_slice_turns_starts_after_last_boundary_and_skips_tool_results(tmp_path):
    transcript = tmp_path / "t.jsonl"
    probe.build_transcript(transcript, str(tmp_path))
    excerpt = pr.slice_turns(str(transcript))
    assert "old topic" not in excerpt  # before the previous boundary
    assert probe.COMPACTION_SUMMARY in excerpt  # the previous summary leads the excerpt
    assert probe.DECISION in excerpt
    assert "x" * 50 not in excerpt  # tool_result blobs are not conversation
    assert pr.slice_turns(str(tmp_path / "missing.jsonl")) == ""


def test_slice_turns_keeps_newest_turns_within_budget(tmp_path):
    transcript = tmp_path / "t.jsonl"
    rows = [{"type": "user", "message": {"role": "user", "content": f"turn-{i:03d} " + "w" * 90}} for i in range(100)]
    transcript.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    excerpt = pr.slice_turns(str(transcript), max_chars=2000)
    assert len(excerpt) <= 2000
    assert "turn-099" in excerpt and "turn-000" not in excerpt


def test_extract_residual_redacts_what_it_sends_and_drops_sensitive_answers():
    seen = {}

    def fake(prompt, text):
        seen["text"] = text
        return json.dumps(
            {"decisions": ["use WAL", f"rotate {probe.SECRET} monthly"], "cards": ["T-1633"], "next_step": "write tests"}
        )

    items = pr.extract_residual(f"user: my token {probe.SECRET} leaked", llm=fake, config={"provider": "ollama", "model": ""})
    assert probe.SECRET not in seen["text"]
    assert [i["text"] for i in items if i["kind"] == "decision"] == ["use WAL"]  # the item carrying a secret is dropped
    assert pr.extract_residual("   ", llm=fake) == []
    assert pr.extract_residual("hello", llm=lambda p, t: "", config={"provider": "x", "model": ""}) == []
    assert pr.extract_residual("hello", llm=lambda p, t: "not json at all", config={"provider": "x", "model": ""}) == []


def test_provider_config_sonnet_on_claude_and_says_so_otherwise():
    claude = pr.provider_config({"MEMORYMASTER_LLM_PROVIDER": "claude_cli"})
    assert claude["model"] == pr.DEFAULT_MODEL
    overridden = pr.provider_config({"MEMORYMASTER_LLM_PROVIDER": "anthropic", pr.ENV_MODEL: "claude-opus-5-5"})
    assert overridden["model"] == "claude-opus-5-5"
    other = pr.provider_config({"MEMORYMASTER_LLM_PROVIDER": "google"})
    assert other["model"] == "" and "not Anthropic" in other["note"] and "as configured" in other["note"]
    assert pr.provider_config({})["provider"] == "google"
    forced = pr.provider_config({"MEMORYMASTER_LLM_PROVIDER": "google", pr.ENV_PROVIDER: "claude_cli"})
    assert forced["provider"] == "claude_cli" and forced["model"] == pr.DEFAULT_MODEL and forced["forced"]


def test_forced_provider_is_scoped_to_the_call_not_the_process(monkeypatch):
    from memorymaster.core import llm_provider

    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "google")
    monkeypatch.setenv("MEMORYMASTER_LLM_MODEL", "some-gemini-model")
    seen = {}

    def fake(prompt, text):
        seen["provider"] = llm_provider._env("MEMORYMASTER_LLM_PROVIDER")
        seen["model"] = llm_provider._env("MEMORYMASTER_LLM_MODEL")
        return json.dumps({"decisions": ["x is decided"]})

    pr.extract_residual("user: hello there", llm=fake, config=pr.provider_config(
        {"MEMORYMASTER_LLM_PROVIDER": "google", pr.ENV_PROVIDER: "anthropic"}))
    assert seen == {"provider": "anthropic", "model": pr.DEFAULT_MODEL}
    assert llm_provider._env("MEMORYMASTER_LLM_PROVIDER") == "google"  # nothing leaked past the call


# --- ingest: candidates only, through the filter and the promotion policy -----

@pytest.fixture
def svc(tmp_path):
    service = MemoryService(str(tmp_path / "residual.db"), workspace_root=str(tmp_path))
    service.init_db()
    service.db_target = str(tmp_path / "residual.db")  # test-only convenience
    return service


def _claims(db: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT c.id, c.text, c.status, c.scope, c.source_agent, c.predicate, "
            "(SELECT group_concat(locator) FROM citations WHERE claim_id = c.id) AS locators FROM claims c"
        ).fetchall()
    finally:
        conn.close()


def test_ingest_residual_makes_candidates_with_session_citation_and_never_confirms(svc, tmp_path):
    items = state.bound_residual(probe.FAKE_ANSWER)
    cwd = str(tmp_path / "workspace" / "SyncWorker")
    accepted = pr.ingest_residual(items, db_path=svc.db_target, session_id="sess-1", cwd=cwd)
    assert accepted == len(items) > 0
    rows = _claims(svc.db_target)
    assert {r["status"] for r in rows} == {"candidate"}
    assert {r["source_agent"] for r in rows} == {pr.SOURCE_AGENT}
    assert {r["scope"] for r in rows} == {"project:syncworker"}  # resolved from cwd
    assert {r["locators"] for r in rows} == {"sess-1"}  # the session id is the citation
    assert {"open_decision", "rejected_option", "unverified_assumption", "in_flight_cards", "next_step"} == {
        r["predicate"] for r in rows
    }
    # Idempotent: a second compaction extracting the same facts adds nothing.
    pr.ingest_residual(items, db_path=svc.db_target, session_id="sess-1", cwd=cwd)
    assert len(_claims(svc.db_target)) == len(rows)
    # The promotion policy refuses every automatic path.
    for row in rows:
        with pytest.raises(PromotionPolicyBlocked):
            transition_claim(svc.store, row["id"], "confirmed", reason="auto", event_type="validator")


def test_ingest_residual_sensitive_filter_runs_on_the_ingest_path_too(svc, tmp_path):
    # drop_sensitive is the first filter; this proves the service filter still guards a bypassed one.
    items = [{"kind": "decision", "text": f"deploy with key {probe.SECRET}"}]
    pr.ingest_residual(items, db_path=svc.db_target, session_id="s", cwd=str(tmp_path))
    assert all(probe.SECRET not in r["text"] for r in _claims(svc.db_target))


def test_ingest_residual_refuses_the_global_scope(svc):
    items = state.bound_residual({"decisions": ["use WAL"]})
    assert pr.ingest_residual(items, db_path=svc.db_target, session_id="s", cwd="") == 0
    assert _claims(svc.db_target) == []


def test_ingest_residual_spool_regime_never_opens_the_database(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_SPOOL_DIR", str(tmp_path / "spool"))
    db = tmp_path / "never-created.db"
    items = state.bound_residual({"decisions": ["use WAL"], "cards": ["T-1633"]})
    accepted = pr.ingest_residual(items, db_path=str(db), session_id="s", cwd=str(tmp_path), spool=True)
    assert accepted == 2 and not db.exists()
    envelopes = [json.loads(line) for f in (tmp_path / "spool").rglob("*") if f.is_file() for line in f.read_text().splitlines()]
    assert envelopes and all(e["payload"]["source_agent"] == pr.SOURCE_AGENT for e in envelopes)


def test_capture_residual_no_turns_no_call(tmp_path):
    calls = []
    outcome = pr.capture_residual(
        {"session_id": "s", "transcript_path": str(tmp_path / "none.jsonl"), "cwd": str(tmp_path)},
        db_path=str(tmp_path / "x.db"),
        llm=lambda p, t: calls.append(1) or "{}",
        state_directory=tmp_path / "state",
    )
    assert outcome["skipped"] == "no-turns" and not calls


def test_capture_residual_respects_the_capture_budget(tmp_path):
    from memorymaster.core.capture_control import CaptureLedger, CaptureLimits

    transcript = tmp_path / "t.jsonl"
    probe.build_transcript(transcript, str(tmp_path))
    ledger = CaptureLedger(tmp_path / "cap.db", limits=CaptureLimits(global_daily_calls=0))
    calls = []
    outcome = pr.capture_residual(
        {"session_id": "s", "transcript_path": str(transcript), "cwd": str(tmp_path)},
        db_path=str(tmp_path / "x.db"),
        llm=lambda p, t: calls.append(1) or "{}",
        state_directory=tmp_path / "state",
        ledger=ledger,
    )
    assert outcome["skipped"] == "capture-budget" and not calls


def test_capture_residual_provider_failure_is_contained(tmp_path):
    transcript = tmp_path / "t.jsonl"
    probe.build_transcript(transcript, str(tmp_path))

    def boom(prompt, text):
        raise RuntimeError("provider down")

    outcome = pr.capture_residual(
        {"session_id": "s", "transcript_path": str(transcript), "cwd": str(tmp_path)},
        db_path=str(tmp_path / "x.db"),
        llm=boom,
        state_directory=tmp_path / "state",
    )
    assert outcome["skipped"] == "llm-error"
    assert not (tmp_path / "state").exists()


# --- the PreCompact hook template -------------------------------------------

@pytest.fixture
def provider():
    fake = probe.FakeProvider()
    yield fake
    fake.close()


def _hook(tmp_path, provider_url, payload, *, residual, extra=None, name="memorymaster-precompact.py"):
    env = probe.hermetic_env(tmp_path, provider_url, residual=residual, extra=extra)
    return probe.run_hook(probe.stage_hook(HOOKS / name, tmp_path), payload, env), env


def _payload(tmp_path, **over):
    transcript = tmp_path / "transcript.jsonl"
    probe.build_transcript(transcript, str(tmp_path / "ws" / "syncworker"))
    return {"session_id": "sess-A", "transcript_path": str(transcript), "cwd": str(tmp_path / "ws" / "syncworker"),
            "trigger": "auto", "custom_instructions": None, **over}


def test_precompact_residual_is_off_by_default(tmp_path, provider):
    result, _ = _hook(tmp_path, provider.url, _payload(tmp_path), residual=False)
    assert result.returncode == 0 and result.stdout == ""
    assert provider.requests == []
    assert not (tmp_path / "hook_state").exists()


def test_precompact_residual_end_to_end(tmp_path, provider):
    service = MemoryService(str(tmp_path / "probe.db"), workspace_root=str(tmp_path))
    service.init_db()  # MEMORYMASTER_DEFAULT_DB points here: a throwaway database
    result, _ = _hook(tmp_path, provider.url, _payload(tmp_path), residual=True)
    assert result.returncode == 0
    assert result.stdout == ""  # never blocks, never talks on stdout
    assert len(provider.requests) == 1  # one call
    assert probe.SECRET not in json.dumps(provider.requests)
    saved = json.loads((tmp_path / "hook_state" / "residual_sess-A.json").read_text())
    assert any(probe.DECISION in item["text"] for item in saved["items"])
    rows = _claims(str(tmp_path / "probe.db"))
    assert rows and {r["status"] for r in rows} == {"candidate"} and {r["source_agent"] for r in rows} == {pr.SOURCE_AGENT}
    assert {r["locators"] for r in rows} == {"sess-A"}
    assert {r["scope"] for r in rows} == {"project:syncworker"}


def test_precompact_residual_failures_never_break_the_compaction(tmp_path):
    for answer in ("this is not json", {"decisions": []}):
        bad = probe.FakeProvider(answer=answer)
        try:
            result, _ = _hook(tmp_path, bad.url, _payload(tmp_path), residual=True)
        finally:
            bad.close()
        assert result.returncode == 0 and result.stdout == ""
    result, _ = _hook(tmp_path, "http://127.0.0.1:9", _payload(tmp_path), residual=True)  # nothing listens
    assert result.returncode == 0 and result.stdout == ""
    assert not (tmp_path / "hook_state" / "residual_sess-A.json").exists()
    result, _ = _hook(tmp_path, "http://127.0.0.1:9", {"session_id": "x", "transcript_path": "/no/such"}, residual=True)
    assert result.returncode == 0


def test_warn_once_behaviour_is_intact_and_residual_waits_for_the_allowed_compaction(tmp_path, provider):
    extra = {"MEMORYMASTER_PRECOMPACT_BLOCKING": "1"}
    first, _ = _hook(tmp_path, provider.url, _payload(tmp_path), residual=True, extra=extra)
    assert json.loads(first.stdout)["decision"] == "block"  # the existing one-time warning
    assert provider.requests == []  # compaction did not happen: no extraction yet
    second, _ = _hook(tmp_path, provider.url, _payload(tmp_path), residual=True, extra=extra)
    assert second.returncode == 0 and second.stdout == ""  # passes through
    assert len(provider.requests) == 1
    assert (tmp_path / "hook_state" / "residual_sess-A.json").exists()
    assert (tmp_path / "hook_state" / "warned_sess-A.marker").exists()


# --- re-injection by the recall hook template -------------------------------

def _install_recall(tmp_path, monkeypatch):
    from memorymaster.surfaces import setup_hooks as setup

    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    hooks = tmp_path / "claude" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    script = hooks / "memorymaster-recall.py"
    script.write_text(
        setup.replace_placeholder((HOOKS / "memorymaster-recall.py").read_text(encoding="utf-8"), project), encoding="utf-8"
    )
    return script


@pytest.fixture
def recall_env(tmp_path, monkeypatch):
    from memorymaster.core import hook_log
    from memorymaster.recall import remote

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(state.ENV_STATE_DIR, str(tmp_path / "state"))
    monkeypatch.setenv("MEMORYMASTER_RECALL_STATE_DIR", str(tmp_path / "recall_state"))
    monkeypatch.setattr(hook_log, "log_hook", lambda *a, **k: None)
    recalled = {"ctx": "", "raise": False, "calls": 0}

    def fake_hook_recall(query, hook_data, *, db_path, timeout=None):
        recalled["calls"] += 1
        if recalled["raise"]:
            raise RuntimeError("recall backend down")
        return recalled["ctx"], "local"

    monkeypatch.setattr(remote, "hook_recall", fake_hook_recall)
    script = _install_recall(tmp_path, monkeypatch)
    return script, recalled


def _run_recall(script, monkeypatch, capsys, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    capsys.readouterr()
    runpy.run_path(str(script), run_name="__main__")
    return capsys.readouterr().out


def _seed(sid="sess-A"):
    state.save_residual(sid, state.bound_residual(probe.FAKE_ANSWER))


def test_first_prompt_after_compaction_injects_the_residual_once_even_for_a_trivial_prompt(recall_env, monkeypatch, capsys):
    script, recalled = recall_env
    _seed()
    out = _run_recall(script, monkeypatch, capsys, {"session_id": "sess-A", "cwd": "ws", "prompt": "continue"})
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert probe.DECISION in context and len(context) <= state.MAX_CHARS
    assert recalled["calls"] == 0  # "continue" is trivial: delivered without any similarity search
    assert not state.residual_path("sess-A").exists()  # consumed
    again = _run_recall(script, monkeypatch, capsys, {"session_id": "sess-A", "cwd": "ws", "prompt": "continue"})
    assert again == ""  # once


def test_residual_and_recall_context_share_one_json_object(recall_env, monkeypatch, capsys):
    script, recalled = recall_env
    recalled["ctx"] = "[mm-1] an unrelated recalled fact"
    _seed()
    out = _run_recall(script, monkeypatch, capsys, {"session_id": "sess-A", "cwd": "ws", "prompt": "what did we decide about retries"})
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]  # one parseable object
    assert context.index("[MemoryMaster residual]") < context.index("[MemoryMaster recall]")
    assert probe.DECISION in context and "an unrelated recalled fact" in context


def test_residual_is_scoped_to_its_session_and_ignores_automated_events(recall_env, monkeypatch, capsys):
    script, recalled = recall_env
    _seed("sess-A")
    assert _run_recall(script, monkeypatch, capsys, {"session_id": "sess-B", "cwd": "ws", "prompt": "continue please"}) == ""
    auto = _run_recall(script, monkeypatch, capsys, {"session_id": "sess-A", "cwd": "ws", "prompt": "<task-notification> done"})
    assert auto == "" and state.residual_path("sess-A").exists()  # an automated event does not burn the marker
    assert _run_recall(script, monkeypatch, capsys, {"cwd": "ws", "prompt": "continue please"}) == ""  # no session id


def test_stale_residual_is_not_injected(recall_env, monkeypatch, capsys):
    script, _ = recall_env
    state.save_residual("sess-A", state.bound_residual(probe.FAKE_ANSWER), now=time.time() - state.TTL_SECONDS - 5)
    assert _run_recall(script, monkeypatch, capsys, {"session_id": "sess-A", "cwd": "ws", "prompt": "continue please"}) == ""


def test_a_failing_recall_does_not_drop_the_residual(recall_env, monkeypatch, capsys):
    script, recalled = recall_env
    recalled["raise"] = True
    _seed()
    out = _run_recall(script, monkeypatch, capsys, {"session_id": "sess-A", "cwd": "ws", "prompt": "what did we decide about retries"})
    assert probe.DECISION in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_recall_without_a_residual_is_unchanged(recall_env, monkeypatch, capsys):
    script, recalled = recall_env
    recalled["ctx"] = "[mm-2] fact"
    out = _run_recall(script, monkeypatch, capsys, {"session_id": "sess-A", "cwd": "ws", "prompt": "explain the importer"})
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == "[MemoryMaster recall]\n[mm-2] fact"


# --- the M2 probe ------------------------------------------------------------

def test_m2_probe_fails_without_the_residual_and_passes_with_it(tmp_path):
    before = probe.run_probe(HOOKS, tmp_path / "off", residual=False)
    after = probe.run_probe(HOOKS, tmp_path / "on", residual=True)
    assert before["passed"] is False and before["injected_chars"] == 0 and before["provider_calls"] == 0
    assert after["passed"] is True and after["provider_calls"] == 1
    assert after["precompact_stdout_empty"] and not after["secret_sent_to_provider"]


# --- installation declares the hook correctly --------------------------------

def test_install_declares_the_precompact_event_with_a_timeout_that_outlasts_the_residual_budget(tmp_path, monkeypatch):
    from memorymaster.surfaces import setup_hooks as setup

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(setup, "PROJECT_ROOT", project)
    monkeypatch.setattr(setup, "CLAUDE_DIR", tmp_path / "claude")
    monkeypatch.setattr(setup, "ask_yn", lambda *a, **k: False)
    setup.install_hooks({"provider": "google", "api_key": "", "model": ""})
    settings = json.loads((tmp_path / "claude" / "settings.json").read_text())
    entries = [h for entry in settings["hooks"]["PreCompact"] for h in entry["hooks"] if "memorymaster-precompact.py" in h["command"]]
    assert len(entries) == 1 and entries[0]["timeout"] >= 90
    template = (HOOKS / "memorymaster-precompact.py").read_text(encoding="utf-8")
    assert "RESIDUAL_BUDGET_SECONDS = 75" in template and entries[0]["timeout"] > 75
