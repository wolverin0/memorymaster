"""Brief collectors: claims, ledger cards, due-measurements. Fixtures live under tmp_path only; the live
DB, the real _intel/tasks ledger and ~/.memorymaster are never read."""

from dataclasses import replace
from datetime import datetime, timezone
import json
import sqlite3

from test_project_repository import claim, project_config as _project_config

from memorymaster.profile import project_brief
from memorymaster.profile import project_runtime as runtime
from memorymaster.profile.project_brief import collect_brief_inputs
from memorymaster.profile.project_projection import (
    build_project_projection,
    publish_project_projection,
    read_manifest,
    read_project_projection,
)
from memorymaster.profile.project_repository import load_brief_claims, load_due_failures, load_ledger

project_config = _project_config
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def write_ledger(config, *cards):
    (config.ledger_root / "tasks.json").write_text(json.dumps({"complete": True, "tasks": [
        {"repo": "demo", "visibility": "public", "version": "1", **c} for c in cards]}))


def test_ledger_cards_carry_brief_fields_without_moving_the_support_hash(project_config):
    base = {"id": "T-1", "state": "blocked", "title": "Wire it"}
    write_ledger(project_config, base)
    plain = load_ledger(project_config)[0]["T-1"]
    write_ledger(project_config, {**base, "blocker": "needs key", "next_action": "ask infra",
                                  "updated_at": "2026-11-08T10:00:00Z"})
    rich = load_ledger(project_config)[0]["T-1"]
    assert (rich["blocker"], rich["next_action"], rich["updated_at"]) == ("needs key", "ask infra", "2026-11-08T10:00:00Z")
    assert rich["support"] == plain["support"], "display-only fields must not invalidate published manifests"


def test_brief_claims_apply_scope_status_citation_and_supersession_gates(project_config):
    config = replace(project_config, aliases=("project:demo-old",))
    claim(config, 1, "Old decision", event_time="2026-10-01T00:00:00+00:00")
    claim(config, 2, "Alias decision", scope="project:demo-old", event_time="2026-10-05T00:00:00+00:00")
    claim(config, 3, "Newest decision", event_time="2026-10-09T00:00:00+00:00")
    claim(config, 4, "Middle decision", claim_type="architecture_decision", event_time="2026-10-07T00:00:00+00:00")
    claim(config, 5, "Fourth decision, cut", event_time="2026-09-01T00:00:00+00:00")
    claim(config, 6, "Candidate decision", status="candidate", event_time="2026-10-10T00:00:00+00:00")
    claim(config, 7, "Other project", scope="project:other", event_time="2026-10-10T00:00:00+00:00")
    claim(config, 8, "Superseded", replaced_by_claim_id=3, event_time="2026-10-10T00:00:00+00:00")
    claim(config, 9, "Generated", source_agent="project-projection", event_time="2026-10-10T00:00:00+00:00")
    claim(config, 10, "Private", visibility="sensitive", event_time="2026-10-10T00:00:00+00:00")
    found, ok = load_brief_claims(config, now=NOW)
    assert ok
    assert [r["id"] for r in found["decision"]] == [3, 4, 2]
    assert found["decision"][0]["text"] == "Newest decision"


def test_brief_claims_require_a_citation_and_current_validity(project_config):
    claim(project_config, 1, "Expired", valid_until="2026-01-01T00:00:00+00:00")
    claim(project_config, 2, "Uncited")
    with sqlite3.connect(project_config.db_path) as conn:
        conn.execute("DELETE FROM citations WHERE claim_id=2")
    claim(project_config, 3, "Fine")
    found, _ = load_brief_claims(project_config, now=NOW)
    assert [r["id"] for r in found["decision"]] == [3]


def test_failure_claims_are_bug_bug_cause_gotcha_newest_first(project_config):
    claim(project_config, 1, "Cache races", claim_type="bug", event_time="2026-10-01T00:00:00+00:00")
    claim(project_config, 2, "Stale index", claim_type="bug_cause", event_time="2026-10-03T00:00:00+00:00")
    claim(project_config, 3, "Mind the cap", claim_type="gotcha", event_time="2026-10-02T00:00:00+00:00")
    claim(project_config, 4, "Not a failure", claim_type="fact", event_time="2026-10-09T00:00:00+00:00")
    found, _ = load_brief_claims(project_config, now=NOW)
    assert [(r["id"], r["text"]) for r in found["failure"]] == [
        (2, "bug_cause: Stale index"), (3, "gotcha: Mind the cap"), (1, "bug: Cache races")]
    assert found["decision"] == []


def test_unreadable_database_is_reported_not_raised(project_config, tmp_path):
    found, ok = load_brief_claims(replace(project_config, db_path=tmp_path / "missing.sqlite"), now=NOW)
    assert not ok and found == {"decision": [], "failure": []}


def test_due_failures_only_fail_rows_missing_file_is_unavailable(tmp_path):
    path = tmp_path / "due.json"
    path.write_text(json.dumps({"items": [
        {"id": "a", "result": "PASS: fine", "done_at": "2026-11-01T00:00:00Z"},
        {"id": "b", "result": "FAIL (not DONE): drift", "done_at": "2026-11-05T00:00:00Z"},
        {"id": "c", "result": None, "status": "pending"},
        {"id": "d", "result": "FAIL: late", "checked_at": "2026-11-07T00:00:00Z"},
        {"id": "e", "result": "T-1 FAIL later in text"},
    ]}))
    rows, mtime = load_due_failures(path)
    assert [(r["id"], r["when"].day) for r in rows] == [("b", 5), ("d", 7)]
    assert mtime is not None
    assert load_due_failures(tmp_path / "nope.json") == (None, None)
    path.write_text("{not json")
    assert load_due_failures(path) == (None, None)


def test_collect_inputs_sources_slug_gate_and_cards(project_config, tmp_path):
    write_ledger(project_config,
                 {"id": "T-1", "state": "running", "title": "Go", "updated_at": "2026-11-08T00:00:00Z", "next_action": "ship"},
                 {"id": "T-2", "state": "failed", "title": "Broke", "blocker": "oom", "updated_at": "2026-11-07T00:00:00Z"})
    claim(project_config, 1, "Ship decision", event_time="2026-11-06T00:00:00+00:00")
    cards = {card["key"]: card for card in load_ledger(project_config)[0].values()}
    due = tmp_path / "due.json"
    due.write_text(json.dumps({"items": [{"id": "x", "result": "FAIL: bad", "done_at": "2026-11-04T00:00:00Z"}]}))
    config = replace(project_config, brief=True, due_measurements=due)
    got = collect_brief_inputs(config, cards, [], NOW)
    assert "due" not in got.sources, "due rows have no repo field: only memorymaster/py-apps may show them"
    assert list(got.sources) == ["ledger", "roadmap", "claims"] and got.sources["roadmap"] is None
    assert got.sources["ledger"].day == 8 and got.sources["claims"].day == 6
    assert sorted(c["id"] for c in got.cards) == ["T-1", "T-2"]
    # slug gate: the same wiring under a memorymaster scope shows the FAIL row, tagged [due:x].
    mm = replace(config, scope="project:memorymaster")
    got = collect_brief_inputs(mm, cards, ["ledger_unavailable"], NOW)
    assert got.sources["ledger"] is None and got.sources["due"] is not None
    assert [e.tag for e in got.failures] == ["due:x"]


def test_built_projection_carries_brief_and_session_start_only_reads_it(project_config, monkeypatch):
    write_ledger(project_config,
                 {"id": "T-1", "state": "running", "title": "Ship brief", "next_action": "open PR",
                  "updated_at": "2026-11-08T00:00:00Z"},
                 {"id": "T-2", "state": "failed", "title": "Crashed", "blocker": "disk full",
                  "updated_at": "2026-11-07T00:00:00Z"})
    for n in (1, 2, 3):
        claim(project_config, n, f"Decision {n}", event_time=f"2026-10-0{n}T00:00:00+00:00")
    claim(project_config, 4, "Race in cache", claim_type="bug", event_time="2026-10-05T00:00:00+00:00")
    config = replace(project_config, brief=True)
    built = build_project_projection(config)
    brief = built.manifest["brief"]
    assert "- T-1 running: Ship brief | next: open PR [T-1]" in brief
    assert "- T-2 failed: disk full [T-2]" in brief
    assert "- bug: Race in cache [mm-4]" in brief and "[mm-3]" in brief
    assert "NEXT\n- none: no roadmap [ROADMAP.md]" in brief
    assert publish_project_projection(config, built)

    def boom(*args, **kwargs):
        raise AssertionError("collector ran on the hook path")

    monkeypatch.setattr(project_brief, "collect_brief_inputs", boom)
    assert read_project_projection(config).manifest["brief"] == brief


def test_refresh_republishes_when_only_the_brief_changes_and_fingerprint_sees_the_sources(project_config, tmp_path):
    due = tmp_path / "due.json"
    due.write_text(json.dumps({"items": []}))
    config = replace(project_config, scope="project:memorymaster", brief=True, due_measurements=due)
    claim(config, 1, "Decision", scope="project:memorymaster")
    assert runtime._refresh_one(config)["status"] == "ok"
    assert "[due:" not in read_manifest(config)["brief"]
    before = runtime.cheap_fingerprint(config)
    due.write_text(json.dumps({"items": [{"id": "q", "result": "FAIL: no", "done_at": "2026-11-08T00:00:00Z"}]}))
    (config.project_root / "ROADMAP.md").write_text("## Now\n- [ ] x\n")
    assert runtime.cheap_fingerprint(config) != before
    assert runtime._refresh_one(config)["status"] == "ok"
    published = read_manifest(config)["brief"]
    assert "[due:q]" in published and "- x [ROADMAP.md#L2]" in published
    assert "roadmap 0d" in published.splitlines()[0]
