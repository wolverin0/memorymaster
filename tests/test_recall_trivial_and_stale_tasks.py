"""T-1433: no recall injection on "ok", and no stale/foreign task in the look-ahead briefing.

Reported: the UserPromptSubmit hook injected the same ~800 tokens into two consecutive
prompts, one of them just "ok", including a look-ahead briefing built from an active task
that was months old and not the lane's. Disposable fixtures only; never the authoritative DB.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from memorymaster.recall import active_tasks, hook_policy, remote

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def _stamp(**delta) -> str:
    return (NOW - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- trivial prompts ----------------------------------------------------------------------


@pytest.mark.parametrize("prompt", [
    "ok", "OK.", "okkk", "okay", "dale", "Dale!!", "daleee", "si", "sí", "sip", "no", "gracias", "muchas gracias",
    "seguí", "segui", "seguí, dale", "sigue", "continuá", "listo", "perfecto", "genial", "ok gracias", "ok?",
    "thanks!", "thx", "yes please", "go ahead", "sounds good", "continue", "got it", "👍", "👍👍 ok", "...", "?", "", "   ",
])
def test_trivial_prompts_are_recognised(prompt):
    assert hook_policy.is_trivial_prompt(prompt)


@pytest.mark.parametrize("prompt", [
    "why 500?", "por qué falla?", "cómo?", "T-1433 status", "ok fix the importer", "seguí con el bug del hook",
    "what is wal", "error?", "bug", "memorymaster/recall", "ok_dale", "q", "1", "3 ok", "gracias, y el deploy?",
    "dale, revisá recall_hook", "ok, ¿qué hace steward?",
])
def test_short_real_prompts_keep_recall(prompt):
    assert not hook_policy.is_trivial_prompt(prompt)


def test_non_string_prompt_is_not_trivial():
    assert not hook_policy.is_trivial_prompt(None)
    assert not hook_policy.is_trivial_prompt(["ok"])


def test_hook_recall_skips_trivial_prompts_without_asking_anyone(monkeypatch):
    import memorymaster.recall.context_hook as context_hook

    asked = []
    monkeypatch.setattr(remote, "remote_recall", lambda *a, **k: asked.append(a) or remote.RemoteRecall("ctx", True))
    monkeypatch.setattr(context_hook, "recall", lambda *a, **k: asked.append(a) or "local ctx")
    assert remote.hook_recall("ok", {"cwd": "x"}, db_path="db") == ("", "trivial")
    assert remote.hook_recall("dale", {"cwd": "x"}, db_path="db") == ("", "trivial")
    assert asked == []
    assert remote.hook_recall("why does the importer fail", {"cwd": "x"}, db_path="db") == ("ctx", "shared")


def test_shared_server_route_is_silent_for_trivial_prompts(monkeypatch):
    starlette_testclient = pytest.importorskip("starlette.testclient")
    import memorymaster.recall.context_hook as context_hook
    from memorymaster.surfaces import mcp_http

    calls = []
    monkeypatch.setattr(context_hook, "recall", lambda q, **kw: calls.append(q) or "warm ctx")
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    client = starlette_testclient.TestClient(mcp_http.create_http_app(token="secret", db_target="test.db", workspace="."))
    auth = {"Authorization": "Bearer secret"}
    trivial = client.post("/hook/recall", json={"query": "ok", "hook_data": {"cwd": "G:/x"}}, headers=auth)
    real = client.post("/hook/recall", json={"query": "why 500?", "hook_data": {"cwd": "G:/x"}}, headers=auth)
    assert trivial.json() == {"ctx": ""} and real.json() == {"ctx": "warm ctx"}
    assert calls == ["why 500?"]


# --- stale / foreign active tasks ---------------------------------------------------------


def _task(task_id: str, status: str, **fields) -> dict:
    return {"id": task_id, "title": task_id, "status": status, "narrative": "", "yaml": {"status": status, **fields}}


def _select(tasks, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("max_age", 48.0)
    return active_tasks.select_active(tasks, **kw)


def test_stale_in_progress_task_is_excluded_and_reported():
    stale = _task("T-81", "in_progress", dispatched_at="2026-04-29T01:00:00Z")
    active, skipped = _select([stale])
    assert active is None
    assert skipped == [{"id": "T-81", "reason": "older than 48h"}]


def test_fresh_task_wins_and_a_stale_one_ahead_of_it_is_passed_over():
    stale = _task("T-81", "in_progress", dispatched_at="2026-04-29T01:00:00Z")
    fresh = _task("T-90", "in_progress", dispatched_at=_stamp(hours=3))
    active, skipped = _select([stale, fresh])
    assert active["id"] == "T-90" and [s["id"] for s in skipped] == ["T-81"]


def test_freshness_uses_the_newest_stamp_and_the_bound_edge():
    touched = _task("T-1", "in_progress", dispatched_at="2026-04-29T01:00:00Z", updated_at=_stamp(hours=47))
    assert _select([touched])[0]["id"] == "T-1"
    assert _select([_task("T-2", "pending", updated_at=_stamp(hours=49))])[0] is None


def test_task_without_stamps_falls_back_to_file_mtime():
    undated = _task("T-82", "pending")
    assert _select([undated], file_mtime=(NOW - timedelta(days=170)).timestamp())[0] is None
    assert _select([undated], file_mtime=(NOW - timedelta(hours=2)).timestamp())[0]["id"] == "T-82"
    assert _select([undated])[0]["id"] == "T-82"  # nothing to judge by: not excluded


def test_expired_lease_excludes_even_a_recent_task():
    leased = _task("T-3", "in_progress", dispatched_at=_stamp(hours=1), lease_until=_stamp(minutes=5))
    assert _select([leased])[1] == [{"id": "T-3", "reason": "lease expired (lease_until)"}]


def test_task_of_another_cwd_or_lane_is_excluded():
    other_cwd = _task("T-4", "in_progress", dispatched_at=_stamp(hours=1), cwd="G:/Py Apps/wezbridge")
    other_lane = _task("T-5", "in_progress", dispatched_at=_stamp(hours=1), owner="claude-l2")
    mine = _task("T-6", "pending", dispatched_at=_stamp(hours=1), owner="claude-l1", cwd="G:\\Py Apps\\memorymaster")
    active, skipped = _select([other_cwd, other_lane, mine], cwd="G:/Py Apps/memorymaster/_wt/x", lane="claude-l1")
    assert active["id"] == "T-6"
    assert {s["id"]: s["reason"] for s in skipped} == {"T-4": "other cwd", "T-5": "other lane"}


def test_completed_tasks_are_never_selected_or_reported():
    done = _task("T-7", "completed", completed_at="2026-04-20T02:30:00Z")
    assert _select([done]) == (None, [])


def test_bound_zero_disables_the_age_check_and_env_sets_the_default(monkeypatch):
    old = _task("T-8", "in_progress", dispatched_at="2026-04-29T01:00:00Z")
    assert _select([old], max_age=0)[0]["id"] == "T-8"
    monkeypatch.setenv(active_tasks.MAX_AGE_ENV, "100000")
    assert active_tasks.select_active([old], now=NOW)[0]["id"] == "T-8"
    monkeypatch.setenv(active_tasks.MAX_AGE_ENV, "garbage")
    assert active_tasks.max_age_hours() == active_tasks.DEFAULT_MAX_AGE_HOURS


def test_read_active_tasks_tool_does_not_return_the_stale_task(tmp_path):
    """The MCP tool the look-ahead hook calls: the April task from the report is not 'active'."""
    from memorymaster.surfaces import mcp_server

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "active_tasks.md").write_text(
        "## Task: extend recall hook\n```yaml\nstatus: in_progress\nowner: claude-l1\n"
        "dispatched_at: 2026-04-29T01:00:00Z\n```\nold narrative\n\n"
        "## Task: current work\n```yaml\nstatus: in_progress\nowner: claude-l1\n"
        f"dispatched_at: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n```\nnew narrative\n",
        encoding="utf-8",
    )
    result = asyncio.run(mcp_server.mcp._tool_manager.call_tool("read_active_tasks", {"project_root": str(tmp_path)}))
    assert result["ok"] is True
    assert result["active_task"]["title"] == "current work"
    assert [s["reason"] for s in result["skipped"]] == ["older than 48h"]
    assert len(result["all_tasks"]) == 2


def test_fresh_active_task_is_a_drop_in_for_a_hooks_own_parser(tmp_path):
    """What a hook that parses active_tasks.md itself calls: (title, narrative) of a fresh task only."""
    path = tmp_path / "active_tasks.md"
    stale_only = (
        "## Task: extend memorymaster-recall hook\n```yaml\nstatus: in_progress\ndispatched_at: 2026-04-29T01:00:00Z\n```\n"
        "old narrative\n\n## Task: build detector\n```yaml\nstatus: pending\n```\nundated narrative\n"
    )
    path.write_text(stale_only, encoding="utf-8")
    old = (NOW - timedelta(days=170)).timestamp()
    os.utime(path, (old, old))
    assert active_tasks.fresh_active_task(path) == (None, None)  # undated pending task inherits the old mtime
    os.utime(path, None)  # the dispatcher touched the file just now: only the dated April task stays stale
    assert active_tasks.fresh_active_task(path) == ("build detector", "undated narrative")
    assert active_tasks.fresh_active_task(tmp_path / "missing.md") == (None, None)
