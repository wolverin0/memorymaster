"""T-1633 (AO-1): the PreCompact residual survives a compaction.

Hermetic: a fake local provider, tmp homes, tmp databases. Never the authoritative database.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
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


def _write_rows(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _msg(role, text, **extra):
    return {"type": role, "message": {"role": role, "content": text}, **extra}


def test_slice_turns_previous_summary_is_capped_by_its_own_limit_not_the_message_limit(tmp_path):
    # Argon review 1: a >1500 char summary was middle-elided to ~1500 before the 2500 cap applied.
    transcript = tmp_path / "t.jsonl"
    summary = "S" * 2000
    _write_rows(transcript, [_msg("user", summary, isCompactSummary=True), _msg("user", "a real new turn")])
    excerpt = pr.slice_turns(str(transcript), max_chars=30000)
    assert "S" * 2000 in excerpt  # whole summary kept (limit 2500), not elided at 1500
    assert "…" not in excerpt


def test_slice_turns_returns_nothing_when_only_the_old_summary_exists(tmp_path):
    # Argon review 2: no turns after the boundary must not trigger an LLM call on the old summary alone.
    transcript = tmp_path / "t.jsonl"
    _write_rows(transcript, [
        {"type": "system", "subtype": "compact_boundary"},
        _msg("user", "previous summary text", isCompactSummary=True),
    ])
    assert pr.slice_turns(str(transcript)) == ""


def test_slice_turns_newest_turns_are_reserved_before_the_summary_prefix(tmp_path):
    transcript = tmp_path / "t.jsonl"
    rows = [_msg("user", "P" * 2400, isCompactSummary=True)]
    rows += [_msg("user", f"turn-{i:03d} " + "w" * 90) for i in range(60)]
    _write_rows(transcript, rows)
    budget = 3000
    excerpt = pr.slice_turns(str(transcript), max_chars=budget)
    assert len(excerpt) <= budget  # the summary prefix, when present, is inside the budget
    assert "turn-059" in excerpt and excerpt.count("turn-") >= 25  # turns are fitted first and not starved


def test_slice_turns_keeps_a_turn_that_fits_even_when_the_summary_would_not(tmp_path):
    # Argon review of #283: the newest turn fits the budget but not budget - len(prefix).
    transcript = tmp_path / "t.jsonl"
    turn_text = "n" * pr.MESSAGE_CHARS  # a full-size message: 1506 chars with its role label
    budget = len("user: ") + len(turn_text) + 10
    _write_rows(transcript, [_msg("user", "P" * 2500, isCompactSummary=True), _msg("user", turn_text)])
    excerpt = pr.slice_turns(str(transcript), max_chars=budget)
    assert turn_text in excerpt and len(excerpt) <= budget
    assert "P" * 100 not in excerpt  # no room left: the summary is dropped, never the turn


def test_slice_turns_adds_a_trimmed_summary_with_the_room_that_is_left(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_rows(transcript, [_msg("user", "P" * 2400, isCompactSummary=True), _msg("user", "short new turn")])
    excerpt = pr.slice_turns(str(transcript), max_chars=3000)
    assert "short new turn" in excerpt and "P" * 100 in excerpt and len(excerpt) <= 3000
    assert excerpt.count("P") <= 3000 // 5  # capped at a fifth of the budget


def test_slice_turns_returns_nothing_when_not_even_the_newest_turn_fits(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_rows(transcript, [_msg("user", "P" * 500, isCompactSummary=True), _msg("user", "n" * 1400)])
    assert pr.slice_turns(str(transcript), max_chars=1000) == ""


def test_extract_residual_checks_secrets_before_the_clip_can_hide_them():
    # Argon review 2: a token straddling the 220 char clip must not slip through truncated.
    token = "ghp_" + "a" * 36
    padded = "x" * 200 + " " + token  # the clip at 220 would cut the token in half
    answer = json.dumps({"decisions": [padded, "keep WAL on"], "next_step": "ok"})
    items = pr.extract_residual("user: hello there", llm=lambda p, t: answer, config={"provider": "x", "model": ""})
    texts = " ".join(i["text"] for i in items)
    assert "ghp_" not in texts and "x" * 50 not in texts  # the whole item is dropped, no truncated token
    assert "keep WAL on" in texts


def test_capture_residual_still_ingests_when_the_marker_cannot_be_written(tmp_path):
    service = MemoryService(str(tmp_path / "r.db"), workspace_root=str(tmp_path))
    service.init_db()
    blocker = tmp_path / "state-is-a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    transcript = tmp_path / "t.jsonl"
    probe.build_transcript(transcript, str(tmp_path / "ws" / "syncworker"))
    outcome = pr.capture_residual(
        {"session_id": "s", "transcript_path": str(transcript), "cwd": str(tmp_path / "ws" / "syncworker")},
        db_path=str(tmp_path / "r.db"),
        llm=lambda p, t: json.dumps(probe.FAKE_ANSWER),
        state_directory=blocker / "child",
    )
    assert outcome["ingested"] > 0 and len(_claims(str(tmp_path / "r.db"))) == outcome["ingested"]


def test_save_residual_returns_none_instead_of_raising_when_the_directory_is_unusable(tmp_path):
    blocker = tmp_path / "f"
    blocker.write_text("x", encoding="utf-8")
    assert state.save_residual("s", state.bound_residual({"decisions": ["d"]}), directory=blocker / "sub") is None


def test_expired_residual_is_deleted_not_reparsed_forever(tmp_path):
    state.save_residual("s", state.bound_residual({"decisions": ["d"]}), directory=tmp_path, now=1000.0)
    path = state.residual_path("s", tmp_path)
    assert state.pending_block("s", directory=tmp_path, now=1000.0 + state.TTL_SECONDS + 1) == ""
    assert not path.exists()


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


def test_prompt_worst_case_answer_fits_the_smallest_provider_output_cap():
    worst_chars = pr.PROMPT_MAX_ITEMS * pr.PROMPT_ITEM_CHARS + 200  # items plus JSON scaffolding
    assert worst_chars / 3 < pr.PROVIDER_MIN_OUTPUT_TOKENS  # pessimistic 3 chars per token
    assert str(pr.PROMPT_MAX_ITEMS) in pr.PROMPT and str(pr.PROMPT_ITEM_CHARS) in pr.PROMPT
    assert pr.PROMPT_MAX_ITEMS <= state.MAX_ITEMS


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


def test_residual_excerpt_is_gated_line_by_line_and_the_outcome_reports_what_was_withheld(tmp_path):
    """Contract: blocked lines never reach the provider; an all-withheld excerpt makes no call; outcome has egress_withheld."""
    blocked = "DB_PASS=" + "hunter2hunter2"
    benign = "The service retries three times before it gives up."
    cfg = {"provider": "openai", "model": "", "forced": False}
    sent: list[str] = []

    def llm(prompt, text):
        sent.append(text)
        return json.dumps({"decisions": [], "rejected": [], "assumptions": [], "cards": [], "next_step": ""})

    stats: dict[str, int] = {}
    pr.extract_residual(f"USER: {blocked}\nASSISTANT: {benign}", llm=llm, config=cfg, stats=stats)
    assert len(sent) == 1 and "hunter2" not in sent[0] and benign in sent[0] and stats["egress_withheld"] == 1
    stats = {}
    assert pr.extract_residual(f"USER: {blocked}", llm=llm, config=cfg, stats=stats) == []
    assert len(sent) == 1 and stats["egress_withheld"] == 1  # all withheld: no provider call
    transcript = tmp_path / "t.jsonl"
    probe.build_transcript(transcript, str(tmp_path))
    outcome = pr.capture_residual(
        {"session_id": "s", "transcript_path": str(transcript), "cwd": str(tmp_path)},
        db_path=str(tmp_path / "x.db"), llm=lambda p, t: "{}", state_directory=tmp_path / "state",
    )
    assert outcome["egress_withheld"] == 0


# --- carry-over: a delivered residual is carried by code, never by the model -------------------------------

OPEN_MARK = "OPENQ"
OPEN_TURN = "should the sync retry use jitter? OPENQ still undecided"
NOTHING_OPEN = json.dumps({"decisions": [], "rejected": [], "assumptions": [], "cards": [], "next_step": ""})


def _echo(prompt, text):
    """A fake model that can only return what the text it is given carries: any line with the marker."""
    found = [line.split(": ", 1)[-1].strip() for line in text.splitlines() if OPEN_MARK in line]
    return json.dumps({"decisions": found})


def _append_rows(path, rows):
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(json.dumps(r) for r in rows) + "\n")


def _session(tmp_path, *, text=OPEN_TURN):
    """A transcript whose first stretch states an open item, plus a way to compact it with unrelated turns."""
    transcript = tmp_path / "t.jsonl"
    _write_rows(transcript, [_msg("user", "start"), _msg("assistant", text), _msg("user", "go on")])

    def compact(*turns):
        _append_rows(transcript, [{"type": "system", "subtype": "compact_boundary"}, _msg("user", "Summary: the session worked on the sync worker.", isCompactSummary=True)])
        _append_rows(transcript, [_msg("user", t) for t in (turns or ("now the logging format", "logging uses json lines"))])

    return transcript, compact


def _capture(tmp_path, transcript, llm, *, db="x.db", cwd=None, **extra):
    return pr.capture_residual(
        {"session_id": "sess-carry", "transcript_path": str(transcript), "cwd": cwd or str(tmp_path)},
        db_path=str(tmp_path / db), llm=llm, state_directory=tmp_path / "state", **extra,
    )


def _deliver(tmp_path):
    """What the prompt hook does on the first prompt after a compaction; returns the delivered block."""
    block = state.pending_block("sess-carry", directory=tmp_path / "state")
    assert block
    state.consume("sess-carry", directory=tmp_path / "state")
    return block


def _previous(tmp_path):
    return state.previous_items("sess-carry", directory=tmp_path / "state")


def _claim_texts(db):
    return [r["text"] for r in _claims(str(db)) if r["source_agent"] == pr.SOURCE_AGENT]


def _cwd(tmp_path):
    return str(tmp_path / "workspace" / "SyncWorker")


def test_an_open_item_only_in_the_delivered_residual_survives_two_more_compactions_then_ends(svc, tmp_path):
    transcript, compact = _session(tmp_path)
    kwargs = {"db": "residual.db", "cwd": _cwd(tmp_path)}
    assert _capture(tmp_path, transcript, _echo, **kwargs)["ingested"] == 1
    carried_line = f"- decision: {OPEN_TURN}"
    for age in (1, 2):
        block = _deliver(tmp_path)
        compact()  # nothing after the boundary restates the item and the summary dropped it
        outcome = _capture(tmp_path, transcript, _echo, **kwargs)
        assert OPEN_TURN in state.pending_block("sess-carry", directory=tmp_path / "state"), "the open item was lost at this compaction"
        assert outcome["items"] == 0 and outcome["carried"] == 1 and outcome["ingested"] == 0  # the model returned nothing
        assert _previous(tmp_path) == [{"kind": "decision", "text": OPEN_TURN, "carried": age}]
        assert state.CARRIED_HEADING in state.pending_block("sess-carry", directory=tmp_path / "state")
        assert carried_line in state.pending_block("sess-carry", directory=tmp_path / "state")
        assert (block.count(OPEN_TURN) == 1)
    _deliver(tmp_path)
    compact()
    assert _capture(tmp_path, transcript, _echo, **kwargs)["skipped"] == "empty-residual"  # carried through two: gone
    assert _previous(tmp_path) == [] and not state.carried_path("sess-carry", tmp_path / "state").exists()
    assert _claim_texts(svc.db_target) == [OPEN_TURN]  # one claim for the whole life of the item


def _rotate(text):
    words = text.split()
    return " ".join(words[2:] + words[:2])


ADVERSARIES = {
    "rotate_words": lambda n, original: {"decisions": [_rotate(original)]},
    "kind_swap_and_rewrite": lambda n, original: {"assumptions": [_rotate(original) + f" v{n}"]},
    "split": lambda n, original: {"decisions": [original[:20] + f" part a{n}", original[20:] + f" part b{n}"]},
    "invented_extras": lambda n, original: {"decisions": [f"invented extra number {n} about the caching layer", f"another invented thing {n} entirely"]},
    "topical_chatter": lambda n, original: {"decisions": [f"we talked about the sync retry and jitter again in round {n}"]},
    "near_miss": lambda n, original: {"decisions": [original.replace("sync", "audit")]},
}


@pytest.mark.parametrize("name", sorted(ADVERSARIES))
def test_no_model_answer_can_change_a_carried_items_text_age_or_ingests(svc, tmp_path, name):
    transcript, compact = _session(tmp_path)
    kwargs = {"db": "residual.db", "cwd": _cwd(tmp_path)}
    _capture(tmp_path, transcript, _echo, **kwargs)
    emitted: list[str] = []

    def adversary(round_no):
        def llm(prompt, text):
            answer = ADVERSARIES[name](round_no, OPEN_TURN)
            emitted.extend(t for v in answer.values() for t in v)
            return json.dumps(answer)

        return llm

    for round_no in (2, 3, 4):
        _deliver(tmp_path)
        compact()
        _capture(tmp_path, transcript, adversary(round_no), **kwargs)
        original = [i for i in _previous(tmp_path) if i["text"] == OPEN_TURN]
        if round_no < 4:  # byte-identical text, the age the CODE counted, whatever the model answered
            assert original == [{"kind": "decision", "text": OPEN_TURN, "carried": round_no - 1}]
        else:
            assert original == []  # nothing is alive past two answered compactions
    assert _claim_texts(svc.db_target).count(OPEN_TURN) == 1
    assert sorted(_claim_texts(svc.db_target)) == sorted({OPEN_TURN, *emitted})  # only what the model itself said is ingested


def test_a_near_miss_new_item_stated_in_the_turns_is_ingested_and_shown_as_its_own_line(svc, tmp_path):
    old, new = "claims store should use WAL mode OPENQ", "audit store should use WAL mode OPENQ"
    transcript, compact = _session(tmp_path, text=old)
    kwargs = {"db": "residual.db", "cwd": _cwd(tmp_path)}
    _capture(tmp_path, transcript, _echo, **kwargs)
    _deliver(tmp_path)
    compact("we also looked at the other database", new)
    outcome = _capture(tmp_path, transcript, _echo, **kwargs)
    assert outcome["ingested"] == 1 and outcome["items"] == 1 and outcome["carried"] == 1
    block = state.pending_block("sess-carry", directory=tmp_path / "state")
    fresh_part, carried_part = block.split(state.CARRIED_HEADING)
    assert f"- {new}" in fresh_part and f"- decision: {old}" in carried_part
    assert sorted(_claim_texts(svc.db_target)) == sorted([old, new])


def test_the_model_never_sees_the_carried_items_and_the_call_is_still_one(tmp_path):
    transcript, compact = _session(tmp_path)
    _capture(tmp_path, transcript, _echo)
    _deliver(tmp_path)
    compact()
    sent = []
    _capture(tmp_path, transcript, lambda p, t: sent.append((p, t)) or NOTHING_OPEN)
    assert len(sent) == 1 and OPEN_TURN not in sent[0][1] and "previously open" not in sent[0][0]
    assert [i["text"] for i in _previous(tmp_path)] == [OPEN_TURN]  # but the next residual carries it


def _item(kind, text, age=0):
    return {"kind": kind, "text": text, **({"carried": age} if age else {})}


def test_fresh_items_take_the_caps_first_and_the_oldest_carried_item_is_dropped_first():
    fresh = [_item("decision", f"fresh {i}") for i in range(4)]
    previous = [_item("assumption", f"old {i}", age=1 if i % 2 else 0) for i in range(10)]
    out = state.carry_forward(fresh, previous)
    assert len(out) == state.MAX_ITEMS and [i["text"] for i in out[:4]] == [f"fresh {i}" for i in range(4)]
    assert all(i["carried"] in (1, 2) for i in out[4:]) and sum(1 for i in out[4:] if i["carried"] == 1) == 5  # the younger ones first
    assert len(state.render_block(out)) <= state.MAX_CHARS
    assert [i for i in state.carry_forward([_item("decision", f"f{i}") for i in range(10)], previous) if i.get("carried")] == []


def test_the_carried_items_fill_only_what_the_char_cap_leaves_and_never_skip_an_older_one_in():
    fresh = [_item("decision", "f" * 170 + str(i)) for i in range(7)]
    younger, older = _item("assumption", "y" * 200, 1), _item("assumption", "short old", 2)
    assert len(state.render_block([*fresh, younger])) > state.MAX_CHARS >= len(state.render_block([*fresh, older]))  # the scenario
    kept = state._fit([*fresh, older, younger])
    assert kept == fresh  # the younger item does not fit, so the older one is not let in behind it
    assert len(state.render_block(state.carry_forward(fresh, [_item("assumption", "tiny", 0)]))) <= state.MAX_CHARS


def test_an_exact_duplicate_drops_only_the_carried_copy_and_nothing_fuzzy_matches():
    previous = [_item("decision", "Keep  WAL on"), _item("card", "T-1234"), _item("assumption", "other thing"), _item("decision", "use WAL mode everywhere", 1)]
    fresh = [_item("decision", "keep wal ON"), _item("card", "T-1234"), _item("decision", "use WAL mode everywhere now")]
    out = state.carry_forward(fresh, previous)
    assert sorted(i["text"] for i in out if not i.get("carried")) == sorted(["keep wal ON", "T-1234", "use WAL mode everywhere now"])  # fresh text kept
    assert sorted(i["text"] for i in out if i.get("carried")) == ["other thing", "use WAL mode everywhere"]  # the near one stays carried
    assert state.carry_forward([_item("assumption", "Keep WAL on")], [_item("decision", "keep wal on")])[1]["carried"] == 1  # other kind: no match


@pytest.mark.parametrize("answer", ["", '{"decisions": ["x', "no json here", '["a plain string"]', "{}", '{"error": {"code": 500}}'])
def test_a_failed_or_unparseable_answer_leaves_the_carry_untouched(tmp_path, answer):
    transcript, compact = _session(tmp_path)
    _capture(tmp_path, transcript, _echo)
    _deliver(tmp_path)
    compact()
    carried = state.carried_path("sess-carry", tmp_path / "state")
    before = carried.read_bytes()
    assert _capture(tmp_path, transcript, lambda p, t: answer)["items"] == 0
    assert carried.read_bytes() == before and not state.residual_path("sess-carry", tmp_path / "state").exists()
    assert _capture(tmp_path, transcript, _echo)["carried"] == 1  # the next answered capture still carries it, age 1


def test_the_carry_survives_a_budget_skip_and_a_provider_error(tmp_path):
    from memorymaster.core.capture_control import CaptureLedger, CaptureLimits

    transcript, compact = _session(tmp_path)
    _capture(tmp_path, transcript, _echo)
    _deliver(tmp_path)
    compact()
    carried = state.carried_path("sess-carry", tmp_path / "state")
    before = carried.read_bytes()

    def boom(prompt, text):
        raise RuntimeError("provider down")

    assert _capture(tmp_path, transcript, boom)["skipped"] == "llm-error" and carried.read_bytes() == before
    ledger = CaptureLedger(tmp_path / "cap.db", limits=CaptureLimits(global_daily_calls=0))
    assert _capture(tmp_path, transcript, _echo, ledger=ledger)["skipped"] == "capture-budget" and carried.read_bytes() == before


def test_an_answered_capture_that_finds_nothing_new_still_carries_and_ages_the_old_items(tmp_path):
    transcript, compact = _session(tmp_path)
    _capture(tmp_path, transcript, _echo)
    _deliver(tmp_path)
    compact()
    outcome = _capture(tmp_path, transcript, lambda p, t: NOTHING_OPEN)
    assert outcome["items"] == 0 and outcome["carried"] == 1 and "skipped" not in outcome
    assert _previous(tmp_path) == [{"kind": "decision", "text": OPEN_TURN, "carried": 1}]


def test_carried_items_pass_the_sensitivity_scrub_before_they_are_shown_again(tmp_path):
    transcript, compact = _session(tmp_path)
    _capture(tmp_path, transcript, _echo)
    _deliver(tmp_path)
    path = state.carried_path("sess-carry", tmp_path / "state")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["items"].append({"kind": "decision", "text": "deploy with key " + probe.SECRET})
    path.write_text(json.dumps(payload), encoding="utf-8")
    compact()
    _capture(tmp_path, transcript, lambda p, t: NOTHING_OPEN)
    block = state.pending_block("sess-carry", directory=tmp_path / "state")
    assert probe.SECRET not in block and OPEN_TURN in block


def _junk_payloads():
    now = time.time()
    return [
        "", "{not json", "[]", json.dumps({"created_at": "x", "items": 5}), json.dumps({"created_at": now, "items": [None, 3, "x", []]}),
        json.dumps({"created_at": now, "items": [{"kind": "card", "text": "not a card id"}, {"kind": "nope", "text": "x"}, {"kind": "decision", "text": 5}]}),
        json.dumps({"created_at": now, "items": [{"kind": "decision", "text": "old", "carried": 99}]}),
    ]


@pytest.mark.parametrize("junk", range(7))
def test_a_corrupt_or_wrong_type_carry_file_means_no_carry_never_an_error(tmp_path, junk):
    transcript, compact = _session(tmp_path)
    compact()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state.carried_path("sess-carry", state_dir).write_text(_junk_payloads()[junk], encoding="utf-8")
    assert _previous(tmp_path) == []
    outcome = _capture(tmp_path, transcript, lambda p, t: NOTHING_OPEN)
    assert "error" not in outcome and outcome.get("carried", 0) == 0


def test_a_huge_carry_file_is_bounded_and_every_item_is_clipped(tmp_path):
    items = [{"kind": "decision", "text": "w" * 400 + str(i), "carried": 1} for i in range(3000)]
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state.carried_path("sess-carry", state_dir).write_text(json.dumps({"created_at": time.time(), "items": items}), encoding="utf-8")
    got = _previous(tmp_path)
    assert len(got) == state.MAX_ITEMS and all(len(i["text"]) <= state.MAX_ITEM_CHARS for i in got)
    assert len(state.render_block(state._fit(state.carry_forward([], got)))) <= state.MAX_CHARS


def test_delivery_keeps_the_marker_as_carried_while_an_expired_one_is_just_deleted(tmp_path):
    items = state.bound_residual({"decisions": ["keep WAL on"]})
    state.save_residual("s", items, directory=tmp_path, now=1000.0)
    state.consume("s", directory=tmp_path)
    assert not state.residual_path("s", tmp_path).exists() and state.carried_path("s", tmp_path).exists()
    assert state.pending_block("s", directory=tmp_path) == ""  # delivered once, never again
    assert [i["text"] for i in state.previous_items("s", directory=tmp_path, now=1000.0 + 60)] == ["keep WAL on"]
    assert state.previous_items("s", directory=tmp_path, now=1000.0 + state.CARRY_TTL_SECONDS + 1) == []
    state.save_residual("e", items, directory=tmp_path, now=1000.0)
    assert state.pending_block("e", directory=tmp_path, now=1000.0 + state.TTL_SECONDS + 1) == ""  # expired: never delivered
    assert not state.residual_path("e", tmp_path).exists()
    assert [i["text"] for i in state.previous_items("e", directory=tmp_path, now=1000.0 + state.TTL_SECONDS + 1)] == ["keep WAL on"]


def test_an_undelivered_marker_wins_over_an_older_carried_one_and_old_carried_files_are_swept(tmp_path):
    now = time.time()
    state.save_residual("s", state.bound_residual({"decisions": ["old one"]}), directory=tmp_path, now=now - 100)
    state.consume("s", directory=tmp_path)
    state.save_residual("s", state.bound_residual({"decisions": ["new one"]}), directory=tmp_path, now=now - 50)
    assert [i["text"] for i in state.previous_items("s", directory=tmp_path)] == ["new one"]
    stale = state.carried_path("other", tmp_path)
    stale.write_text("{}", encoding="utf-8")
    os.utime(stale, (now - state.CARRY_TTL_SECONDS - 10, now - state.CARRY_TTL_SECONDS - 10))
    state.sweep_carried(directory=tmp_path)
    assert not stale.exists() and state.carried_path("s", tmp_path).exists()


def test_a_failed_rename_never_leaves_the_stale_carried_copy_behind(tmp_path, monkeypatch):
    state.save_residual("s", state.bound_residual({"decisions": ["old one"]}), directory=tmp_path)
    state.consume("s", directory=tmp_path)  # carried: old one
    state.save_residual("s", state.bound_residual({"decisions": ["new one"]}), directory=tmp_path)

    def boom(src, dst):
        raise PermissionError("held open")

    monkeypatch.setattr(state.os, "replace", boom)
    state.consume("s", directory=tmp_path)  # must not raise
    monkeypatch.undo()
    assert not state.carried_path("s", tmp_path).exists() and not state.residual_path("s", tmp_path).exists()
    assert state.previous_items("s", directory=tmp_path) == []


def test_a_marker_that_expires_undelivered_does_not_take_the_carry_with_it(tmp_path):
    transcript, compact = _session(tmp_path)
    _capture(tmp_path, transcript, _echo)
    _deliver(tmp_path)
    compact()
    assert _capture(tmp_path, transcript, _echo)["carried"] == 1  # age 1, saved, never delivered
    assert state.pending_block("sess-carry", directory=tmp_path / "state", now=time.time() + state.TTL_SECONDS + 1) == ""
    assert not state.residual_path("sess-carry", tmp_path / "state").exists()  # the expired marker is gone, the lineage is not
    compact()
    assert _capture(tmp_path, transcript, _echo)["carried"] == 1
    assert _previous(tmp_path) == [{"kind": "decision", "text": OPEN_TURN, "carried": 2}]  # the count stayed strict


@pytest.mark.parametrize("echo", [lambda text: text, lambda text: text.upper(), lambda text: "  ".join(text.split())])
def test_a_model_that_re_emits_the_exact_text_makes_a_fresh_item_never_a_second_claim_or_a_carried_one(svc, tmp_path, echo):
    # Rule 5: only an exact (case/whitespace-insensitive) duplicate of a carried item replaces the carried copy. The
    # model never sees carried text, so it can only do this when the turns state the item again; it is then a fresh
    # item exactly as on main (age 0, shown as its own line, one claim), not something the carry keeps alive.
    transcript, compact = _session(tmp_path)
    kwargs = {"db": "residual.db", "cwd": _cwd(tmp_path)}
    _capture(tmp_path, transcript, _echo, **kwargs)
    for _ in range(4):
        _deliver(tmp_path)
        compact()
        _capture(tmp_path, transcript, lambda p, t: json.dumps({"decisions": [echo(OPEN_TURN)]}), **kwargs)
        block = state.pending_block("sess-carry", directory=tmp_path / "state")
        assert state.CARRIED_HEADING not in block and block.lower().count("openq") == 1
        assert all(not i["carried"] for i in _previous(tmp_path))
    assert len(_claim_texts(svc.db_target)) == 1
