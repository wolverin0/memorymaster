"""T-2278 NEXT fallback: a project with no ROADMAP.md shows its top ready/running ledger card; the ledger repo
match is case-insensitive; the acceptance script has a strict mode. Invented fixtures under tmp_path only."""

from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from test_project_repository import project_config as _project_config

from scripts import accept_project_brief as accept
from memorymaster.profile.project_brief import BRIEF_MAX_BYTES, BriefInputs, Entry, render_brief
from memorymaster.profile.project_projection import build_project_projection
from memorymaster.profile.project_repository import load_ledger

project_config = _project_config
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
NO_ROADMAP = "- none: no roadmap [ROADMAP.md]"


def card(id, state, title="Title", updated="2026-11-08T10:00:00Z", **extra):
    return {"id": id, "state": state, "title": title, "updated_at": updated, **extra}


def next_section(brief):
    lines = brief.split("\n")
    return lines[lines.index("NEXT") + 1 : lines.index("FAILURES")]


def brief_of(*cards, **changes):
    return render_brief(BriefInputs(project="demo", as_of=NOW, cards=tuple(cards), **changes))


# --- rendering -----------------------------------------------------------------------------------

def test_no_roadmap_next_is_the_top_ready_card_not_already_in_running():
    cards = (
        card("T-1", "running", "Run now", "2026-11-08T09:00:00Z"),
        card("T-2", "ready", "Ready newest", "2026-11-08T08:00:00Z"),
        card("T-3", "ready", "Ready mid", "2026-11-07T08:00:00Z"),
        card("T-4", "ready", "Ready oldest, unlisted", "2026-11-01T08:00:00Z"),
        card("T-5", "ready", "Ready older, unlisted", "2026-11-02T08:00:00Z"),
    )
    brief = brief_of(*cards)
    running = brief.split("\n")[brief.split("\n").index("RUNNING") + 1 : brief.split("\n").index("NEXT")]
    assert [line.rsplit(" [", 1)[1] for line in running] == ["T-1]", "T-2]", "T-3]"], "RUNNING is unchanged"
    assert next_section(brief) == ["- Ready older, unlisted (ledger: no ROADMAP.md) [T-5]"]


def test_no_roadmap_next_falls_back_to_the_top_card_when_running_lists_them_all():
    brief = brief_of(card("T-1", "running", "Run", "2026-11-08T09:00:00Z"),
                     card("T-2", "ready", "Ready one", "2026-11-08T08:00:00Z"))
    assert next_section(brief) == ["- Ready one (ledger: no ROADMAP.md) [T-2]"]


def test_no_roadmap_next_uses_a_running_card_when_nothing_is_ready():
    cards = tuple(card(f"T-{n}", "running", f"Run {n}", f"2026-11-0{n}T09:00:00Z") for n in (1, 2, 3, 4))
    assert next_section(brief_of(*cards)) == ["- Run 1 (ledger: no ROADMAP.md) [T-1]"]


@pytest.mark.parametrize("cards", [(), (card("T-1", "blocked", "x", blocker="b"), card("T-2", "done"), card("T-3", "review"))])
def test_no_roadmap_and_no_ready_or_running_card_keeps_none(cards):
    assert next_section(brief_of(*cards)) == [NO_ROADMAP]


def test_unavailable_ledger_keeps_none():
    brief = render_brief(BriefInputs(project="demo", as_of=NOW, unavailable={"RUNNING": "ledger source unreadable"}))
    assert next_section(brief) == [NO_ROADMAP]


def test_a_roadmap_file_changes_nothing_with_or_without_open_items():
    cards = (card("T-1", "running", "Run"), card("T-2", "ready", "Ready"))
    assert next_section(brief_of(*cards, roadmap_missing=False)) == ["- none: no open item [ROADMAP.md]"]
    assert next_section(brief_of(*cards, roadmap_missing=False, next_item=("Ship it", 7))) == [
        "- Ship it [ROADMAP.md#L7]"]
    # a roadmap item wins even if the flag says the file is missing (never both)
    assert next_section(brief_of(*cards, next_item=("Ship it", 7))) == ["- Ship it [ROADMAP.md#L7]"]


def test_card_title_cannot_forge_a_tag_or_break_the_line():
    line = next_section(brief_of(card("T-1", "ready", "x [T-999]\n## NEXT | y")))[0]
    assert line.count("[") == 1 and line.endswith("[T-1]") and "\n" not in line and "|" not in line


def test_worst_case_brief_still_fits_with_decisions_cut_first():
    long = "ñandú " * 40
    cards = tuple(card(f"T-{n}", "running", long, f"2026-11-0{n}T00:00:00Z", next_action=long, blocker=long)
                  for n in (1, 2, 3)) + (card("T-9", "ready", long, "2026-11-01T00:00:00Z"),)
    brief = brief_of(
        *cards,
        decisions=tuple(Entry(datetime(2026, 11, n, tzinfo=timezone.utc), long, f"mm-{n}") for n in (1, 2, 3)),
        failures=tuple(Entry(datetime(2026, 11, n, tzinfo=timezone.utc), long, f"mm-{n}") for n in (4, 5, 6)),
        constraints=tuple(Entry(None, long, f"claim:{n}") for n in (1, 2, 3)),
    )
    assert len(brief.encode("utf-8")) <= BRIEF_MAX_BYTES
    lines = brief.split("\n")
    assert next_section(brief)[0].endswith(" (ledger: no ROADMAP.md) [T-9]")
    assert "DECISIONS" not in lines, "a cut drops decisions first"
    assert lines.count("FAILURES") == 1 and len(lines) - lines.index("FAILURES") > 1 and lines[-1].endswith("]")


def test_worst_case_next_line_is_bounded():
    line = next_section(brief_of(card("T-12345", "ready", "ñ" * 500)))[0]
    assert line.endswith(" (ledger: no ROADMAP.md) [T-12345]") and len(line.encode("utf-8")) <= 200


# --- collection: blind cards, case-insensitive repo ----------------------------------------------

def write_ledger(config, *cards):
    (config.ledger_root / "tasks.json").write_text(json.dumps({"complete": True, "tasks": [
        {"repo": "demo", "visibility": "public", "version": "1", **c} for c in cards]}))


def test_projection_with_no_roadmap_never_picks_a_blind_or_private_card(project_config):
    write_ledger(
        project_config,
        {"id": "T-1", "state": "ready", "title": "Public ready", "updated_at": "2026-11-01T00:00:00Z"},
        {"id": "T-2", "state": "ready", "title": "Blind newest", "updated_at": "2026-11-08T00:00:00Z", "blind": True},
        {"id": "T-3", "state": "ready", "title": "Private newest", "updated_at": "2026-11-08T00:00:00Z",
         "visibility": "private"},
        {"id": "T-4", "state": "running", "title": "Is blind", "updated_at": "2026-11-08T00:00:00Z", "is_blind": True},
    )
    built = build_project_projection(replace(project_config, brief=True))
    brief = built.manifest["brief"]
    assert next_section(brief) == ["- Public ready (ledger: no ROADMAP.md) [T-1]"]
    assert "Blind" not in brief and "Private" not in brief and "T-4" not in brief


def test_projection_with_a_roadmap_file_keeps_the_roadmap_line(project_config):
    write_ledger(project_config, {"id": "T-1", "state": "ready", "title": "Ready", "updated_at": "2026-11-01T00:00:00Z"})
    (project_config.project_root / "ROADMAP.md").write_text("## Now\n- [ ] Ship the brief\n", encoding="utf-8")
    brief = build_project_projection(replace(project_config, brief=True)).manifest["brief"]
    assert next_section(brief) == ["- Ship the brief [ROADMAP.md#L2]"]


@pytest.mark.parametrize("mapping,spelled", [("futuramax", "futuraMAX"), ("FUTURAMAX", "futuramax"), ("demo", "demo")])
def test_ledger_repo_matches_the_cards_repo_case_insensitively(project_config, mapping, spelled):
    (project_config.ledger_root / "tasks.json").write_text(json.dumps({"complete": True, "tasks": [
        {"id": "T-1", "repo": spelled, "state": "ready", "title": "Mine", "version": "1"},
        {"id": "T-2", "repo": "someone-else", "state": "ready", "title": "Not mine", "version": "1"}]}))
    cards, _, diagnostics = load_ledger(replace(project_config, ledger_repo=mapping))
    assert sorted(cards) == ["T-1"], diagnostics


def test_ledger_repo_case_fix_reaches_the_brief(project_config):
    (project_config.ledger_root / "tasks.json").write_text(json.dumps({"complete": True, "tasks": [
        {"id": "T-1", "repo": "futuraMAX", "state": "ready", "title": "Mine", "version": "1"}]}))
    config = replace(project_config, ledger_repo="futuramax", brief=True)
    brief = build_project_projection(config).manifest["brief"]
    assert "RUNNING\n- T-1 ready: Mine [T-1]" in brief
    assert next_section(brief) == ["- Mine (ledger: no ROADMAP.md) [T-1]"]


# --- acceptance script: loose vs strict ----------------------------------------------------------

def context(next_line, failures=3):
    fails = [f"- bug {n} [mm-{n}]" for n in range(failures)] or ["- none [claims]"]
    return "\n".join(["RUNNING", "- T-1 running: Go [T-1]", "NEXT", next_line, "FAILURES", *fails, "DECISIONS", "- none [claims]"])


def test_loose_mode_accepts_no_roadmap_and_one_failure():
    assert accept.check(context(NO_ROADMAP, failures=1)) == {"card": True, "roadmap": True, "failure": True}


@pytest.mark.parametrize("line,ok", [
    (NO_ROADMAP, False),
    ("- none: no open item [ROADMAP.md]", True),
    ("- Ship it [ROADMAP.md#L7]", True),
    ("- Ship it (ledger: no ROADMAP.md) [T-1234]", True),
    ("- Ship it", False),
])
def test_strict_roadmap_fact(line, ok):
    assert accept.check(context(line), strict=True)["roadmap"] is ok


@pytest.mark.parametrize("line", ["- Ship it (ledger: no ROADMAP.md) [T-1234]", "- Ship it [ROADMAP.md#L7]"])
def test_loose_roadmap_fact_accepts_ledger_tag_and_roadmap_tags(line):
    assert accept.check(context(line))["roadmap"] is True


@pytest.mark.parametrize("failures,ok", [(0, False), (1, False), (2, False), (3, True)])
def test_strict_failures_need_three_sourced_lines(failures, ok):
    assert accept.check(context("- Ship it [ROADMAP.md#L7]", failures), strict=True)["failure"] is ok


def test_strict_flag_is_printed_and_in_json(monkeypatch, capsys):
    config = type("Config", (), {"slug": "demo", "project_root": ".", "db_path": "."})()
    seen = []

    def fake_accept(configs, clis=accept.CLIS, strict=False):
        seen.append(strict)
        return [{"project": "demo", "cli": "claude", "bytes": 1, "card": True, "roadmap": True, "failure": True,
                 "pass": True}]

    from memorymaster.profile import project_runtime
    monkeypatch.setattr(project_runtime, "load_project_configs", lambda: [config])
    monkeypatch.setattr(accept, "accept", fake_accept)
    assert accept.main(["--strict"]) == 0
    assert "passed 1/1 (strict)" in capsys.readouterr().out
    assert accept.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "loose"
    assert accept.main(["--strict", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "strict" and seen == [True, False, True]
