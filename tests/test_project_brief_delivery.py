"""T-2278 PR6-7: every CLI adapter delivers the published project brief at SessionStart, and the acceptance
runner checks it. Fixtures live under tmp_path; nothing reads the live DB, ledger or hook state."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path

import pytest
from test_project_brief_sources import write_ledger
from test_project_repository import claim
from test_project_repository import project_config as _project_config

from memorymaster.profile import project_runtime
from memorymaster.profile.project_brief import BriefInputs, Entry, render_brief_cut
from memorymaster.profile.project_brief import BRIEF_MAX_BYTES
from memorymaster.profile.project_projection import build_project_projection, publish_project_projection
from memorymaster.surfaces import agy_hooks, hook_recall
from scripts import accept_project_brief as accept

project_config = _project_config


@pytest.fixture()
def published(project_config, tmp_path, monkeypatch):
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.setenv("MEMORYMASTER_RECALL_STATE_DIR", str(tmp_path / "recall-state"))
    monkeypatch.setenv("MEMORYMASTER_HOOK_EVENTS_LOG", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("MEMORYMASTER_HOOK_RECALL_REMOTE", "0")
    monkeypatch.delenv("AGY_BATCH_PROFILE", raising=False)
    monkeypatch.delenv(agy_hooks.INTERNAL_ENV, raising=False)
    for suffix in ("ENABLED", "SESSION_START"):
        monkeypatch.setenv(project_runtime.PREFIX + suffix, "1")
    write_ledger(project_config, {"id": "T-1", "state": "running", "title": "Ship brief", "next_action": "open PR",
                                  "updated_at": "2026-11-08T00:00:00Z"})
    for n in (1, 2, 3):
        claim(project_config, n, f"Decision {n}", event_time=f"2026-10-0{n}T00:00:00+00:00")
    claim(project_config, 4, "Race in cache", claim_type="bug", event_time="2026-10-05T00:00:00+00:00")
    (project_config.project_root / "ROADMAP.md").write_text("## Now\n- [ ] Ship the brief\n", encoding="utf-8")
    config = replace(project_config, brief=True)
    assert publish_project_projection(config, build_project_projection(config))
    monkeypatch.setattr(project_runtime, "load_project_configs", lambda: [config])
    return config


def _emitted(cli: str, config) -> str:
    return accept.render(cli, str(config.project_root), str(config.db_path))


@pytest.mark.parametrize("cli", accept.CLIS)
def test_every_adapter_delivers_the_brief_instead_of_the_legacy_sections(cli, published) -> None:
    text = _emitted(cli, published)
    assert "T-1 running: Ship brief" in text and "[ROADMAP.md#L2]" in text and "bug: Race in cache [claim:4]" in text
    assert "## Purpose" not in text and "## Decisions" not in text  # the legacy block is replaced
    if cli == "codex":
        assert len(text.encode()) <= 1800 and "[... truncated]" not in text


def test_without_a_published_brief_every_adapter_still_gets_the_legacy_payload(published, monkeypatch) -> None:
    """Opt-in: projections on but no brief in the manifest (brief flag off at publish time) changes nothing."""
    config = replace(published, brief=False, output_root=published.output_root.parent / "out-legacy")
    assert publish_project_projection(config, build_project_projection(config))
    monkeypatch.setattr(project_runtime, "load_project_configs", lambda: [config])
    from memorymaster.profile.project_projection import read_project_projection

    projection = read_project_projection(config)
    assert "brief" not in projection.manifest and project_runtime.delivery_payload(projection) == projection.payload
    for cli in accept.CLIS:
        text = _emitted(cli, config)
        assert "Brief demo" not in text and "## Decisions" in text, cli


def test_acceptance_runner_passes_every_cli_for_a_complete_project(published) -> None:
    rows = accept.accept([published])
    assert [(r["cli"], r["pass"]) for r in rows] == [(cli, True) for cli in accept.CLIS], rows


def test_acceptance_runner_flags_a_missing_failure_and_a_silent_adapter(published, monkeypatch) -> None:
    assert accept.check("RUNNING\n- none [ledger]\nNEXT\n- none: no roadmap [ROADMAP.md]\nFAILURES\n- none [claims]") == {
        "card": False, "roadmap": True, "failure": False}
    monkeypatch.delenv(project_runtime.PREFIX + "SESSION_START")  # projections off: adapters emit nothing
    assert not any(r["pass"] for r in accept.accept([published], ["claude", "opencode"]))


def test_claude_template_writes_the_brief_not_the_legacy_block(published, monkeypatch, capsys) -> None:
    path = Path(__file__).resolve().parents[1] / "memorymaster/config_templates/hooks/memorymaster-session-start.py"
    spec = importlib.util.spec_from_file_location("brief_start", path)
    start = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(start)
    monkeypatch.setattr(start, "_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(published.project_root), "session_id": "c1"})))
    with pytest.raises(SystemExit):
        start.main()
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert context.count("<!-- generated by MemoryMaster") == 1 and "FAILURES" in context and "## Purpose" not in context


def test_agy_session_start_without_projections_keeps_the_scope_recall(tmp_path, monkeypatch) -> None:
    from memorymaster.recall import remote

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MEMORYMASTER_HOOK_EVENTS_LOG", str(tmp_path / "events.jsonl"))
    monkeypatch.delenv(project_runtime.PREFIX + "ENABLED", raising=False)
    monkeypatch.setattr(remote, "hook_recall", lambda query, data, *, db_path: ("- [mm-7] fact", "local"))
    out = agy_hooks.run_hook("SessionStart", {"conversationId": "s", "workspacePaths": [str(tmp_path)]},
                             db_path=str(tmp_path / "m.db"))
    assert "[mm-7] fact" in json.loads(out)["injectSteps"][0]["ephemeralMessage"]


def _opencode(published, monkeypatch, capsys, *argv: str, **payload) -> str:
    """The python process the OpenCode plugin spawns: `--session-start` for the brief, no flag for a prompt's recall."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "prompt": "what did we decide about the cache layer design", "cwd": str(published.project_root), **payload})))
    monkeypatch.setattr(hook_recall, "hook_recall", lambda *args, **kwargs: ("- [mm-9] recalled fact", "local"))
    assert hook_recall.main(["--cli", "opencode", *argv]) == 0
    return capsys.readouterr().out


def test_opencode_brief_comes_from_session_start_and_never_rides_a_prompt(published, monkeypatch, capsys) -> None:
    """One delivery path: the plugin asks `--session-start` once per session (test_opencode_hooks counts those);
    the per-prompt process carries the recall only, so the brief cannot arrive twice."""
    brief = _opencode(published, monkeypatch, capsys, "--session-start", session_id="oc-1")
    assert brief.startswith(hook_recall.BRIEF_PREFIX) and "T-1 running: Ship brief" in brief and "mm-9" not in brief
    prompt = _opencode(published, monkeypatch, capsys, session_id="oc-1")
    assert "mm-9" in prompt and "T-1 running" not in prompt and "Brief demo" not in prompt


def test_a_maximum_size_brief_fits_the_codex_cap_with_its_envelope() -> None:
    brief = chr(10).join(f"- {chr(120) * 78} [mm-{n}]" for n in range(20))[:1600]
    context = project_runtime.compose_session_context(brief)
    assert len(brief.encode()) == 1600 and len(context.encode()) <= 1800


# --- review fixes: constraints in the brief, OpenCode brief isolation -------------------------------------


@pytest.mark.parametrize("cli", accept.CLIS)
def test_pinned_constraints_ride_in_the_brief_before_decisions(cli, published) -> None:
    claim(published, 5, "Never write to the live database", claim_type="constraint", pinned=1)
    assert publish_project_projection(published, build_project_projection(published))
    text = _emitted(cli, published)
    assert text.index("CONSTRAINTS") < text.index("DECISIONS")
    assert "- Never write to the live database [claim:5]" in text
    if cli == "codex":
        assert len(text.encode()) <= 1800 and "[... truncated]" not in text


def test_a_pinned_constraint_outlives_decisions_under_the_byte_cap_and_failures_outlive_it() -> None:
    entries = [Entry(datetime(2026, 11, n, tzinfo=timezone.utc), f"item {n} " + "y" * 105, f"mm-{n}") for n in range(1, 4)]
    cards = tuple({"id": f"T-{n}", "state": "running", "title": "t" * 60, "next_action": "n" * 60, "blocker": "b" * 50,
                   "updated_at": "2026-11-08T00:00:00Z"} for n in range(1, 4))
    inputs = BriefInputs(project="demo", as_of=datetime(2026, 11, 9, tzinfo=timezone.utc), cards=cards,
                         next_item=("z" * 110, 7), roadmap_missing=False, decisions=tuple(entries),
                         failures=tuple(entries), constraints=(Entry(None, "Never write the live database", "mm-9"),))
    text, cut = render_brief_cut(inputs)
    assert cut and len(text.encode()) <= BRIEF_MAX_BYTES
    assert "- Never write the live database [mm-9]" in text
    assert text.index("FAILURES") < text.index("CONSTRAINTS") < text.index("DECISIONS")  # required elements first
    assert text.count("[mm-") < 7  # decisions lost lines, the constraint did not


def test_a_failing_brief_prints_nothing_and_never_removes_the_prompts_recall(published, monkeypatch, capsys) -> None:
    def boom(data):
        raise RuntimeError("brief broke")

    monkeypatch.setattr(project_runtime, "session_start_context", boom)
    assert _opencode(published, monkeypatch, capsys, "--session-start", session_id="oc-1") == ""
    assert "mm-9" in _opencode(published, monkeypatch, capsys, session_id="oc-1")


def test_a_lane_with_the_hook_disabled_gets_no_brief(published, monkeypatch, capsys) -> None:
    monkeypatch.setattr(hook_recall, "disabled_for", lambda cwd: True)
    assert _opencode(published, monkeypatch, capsys, "--session-start", session_id="oc-1") == ""


def test_an_opencode_session_without_an_id_gets_no_brief(published, monkeypatch, capsys) -> None:
    assert _opencode(published, monkeypatch, capsys, "--session-start") == ""  # no session_id at all
