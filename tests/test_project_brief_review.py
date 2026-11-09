"""Review fixes for the brief (PR #376): index choice, per-query deadlines, republish on state change,
roadmap root, loader strictness, degrade-not-raise budgets. tmp fixtures only."""

from dataclasses import replace
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from test_project_repository import claim, project_config as _project_config

from memorymaster.profile import project_repository as repository
from memorymaster.profile import project_runtime as runtime
from memorymaster.profile.project_brief import BriefInputs, collect_brief_inputs, render_brief, render_brief_cut
from memorymaster.profile.project_projection import (
    publish_project_projection,
    read_manifest,
    render_project_projection,
)
from memorymaster.profile.project_repository import brief_claims_sql, digest, load_brief_claims

project_config = _project_config
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def gen(config):
    return digest([config.scope, config.context_key, config.epoch, NOW.isoformat(), {}])


def with_indexes(config):
    """The live schema: migration 0031's tuple_key index plus the replaced_by index that tempts the planner."""
    with sqlite3.connect(config.db_path) as conn:
        conn.execute("ALTER TABLE claims ADD COLUMN tuple_key TEXT")
        conn.execute("CREATE INDEX idx_claims_tuple_key ON claims(tenant_id, scope, tuple_key)")
        conn.execute("CREATE INDEX idx_claims_replaced_by ON claims(replaced_by_claim_id)")


def plan(config, **kwargs) -> str:
    with sqlite3.connect(config.db_path) as conn:
        sql = brief_claims_sql(2, 2, **kwargs)
        return " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, ["x", "a", "b", "d", "e", "d", "e", 60]))


def test_claims_query_is_pinned_to_the_tenant_scope_index(project_config):
    """Live DB: the default plan used idx_claims_replaced_by (~35k rows, ~2 s cold); the pinned plan took 2-100 ms."""
    with_indexes(project_config)
    pinned = plan(project_config, indexed=True)
    assert "idx_claims_tuple_key (tenant_id=? AND scope=?)" in pinned
    assert "idx_claims_replaced_by" not in pinned


def test_brief_claims_work_with_and_without_the_index(project_config):
    claim(project_config, 1, "Decision one", event_time="2026-10-01T00:00:00+00:00")
    assert [r["id"] for r in load_brief_claims(project_config, now=NOW)[0]["decision"]] == [1]
    with_indexes(project_config)
    found, ok = load_brief_claims(project_config, now=NOW)
    assert ok and [r["id"] for r in found["decision"]] == [1]


def test_every_source_query_gets_its_own_deadline(project_config, monkeypatch):
    claim(project_config, 1, "Decision one")
    claim(project_config, 2, "Race", claim_type="bug")
    armed = []
    real = sqlite3.connect

    class Spy:
        def __init__(self, conn):
            object.__setattr__(self, "_conn", conn)

        def set_progress_handler(self, *args):
            armed.append(args)
            return self._conn.set_progress_handler(*args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __setattr__(self, name, value):
            setattr(self._conn, name, value)

    monkeypatch.setattr(repository.sqlite3, "connect", lambda *a, **k: Spy(real(*a, **k)))
    assert load_brief_claims(project_config, now=NOW)[1]
    assert len(armed) == 4, "decision query, its citations, failure query, its citations: one deadline each"


@pytest.mark.parametrize("change", [
    "UPDATE claims SET status='archived' WHERE id=1",             # forget (archive transition)
    "UPDATE claims SET visibility='sensitive' WHERE id=1",         # privatized
    "UPDATE claims SET replaced_by_claim_id=2 WHERE id=1",         # superseded
])
def test_state_changes_move_the_fingerprint_and_the_refresh_drops_the_line(project_config, change):
    config = replace(project_config, brief=True)
    claim(config, 1, "Shown decision", event_time="2026-11-01T00:00:00+00:00")
    claim(config, 2, "Other decision", event_time="2026-10-01T00:00:00+00:00")
    assert runtime._refresh_one(config)["status"] == "ok"
    assert "[mm-1]" in read_manifest(config)["brief"]
    before = runtime.cheap_fingerprint(config)
    with sqlite3.connect(config.db_path) as conn:
        conn.execute(change)
    assert runtime.cheap_fingerprint(config) != before, "the refresher must notice the state change"
    assert runtime._refresh_one(config)["status"] == "ok"
    brief = read_manifest(config)["brief"]
    assert "[mm-1]" not in brief and "[mm-2]" in brief


def test_roadmap_root_overrides_project_root_and_is_watched(project_config, tmp_path):
    (project_config.project_root / "ROADMAP.md").write_text("## Now\n- [ ] stale checkout item\n")
    live = tmp_path / "live-main"
    live.mkdir()
    (live / "ROADMAP.md").write_text("## Delivery-first program\n- [ ] 0.2 live main item\n")
    config = replace(project_config, brief=True)
    assert collect_brief_inputs(config, {}, [], NOW).next_item[0] == "stale checkout item"
    pointed = replace(config, roadmap_root=live)
    assert collect_brief_inputs(pointed, {}, [], NOW).next_item == ("0.2 live main item", 2)
    before = runtime.cheap_fingerprint(pointed)
    (live / "ROADMAP.md").write_text("## Delivery-first program\n- [ ] changed\n")
    assert runtime.cheap_fingerprint(pointed) != before


def env(monkeypatch, tmp_path, project, **extra):
    base = {
        "MEMORYMASTER_PROJECT_PROJECTION_ENABLED": "1",
        "MEMORYMASTER_PROJECT_PROJECTION_EPOCH": "e1",
        "MEMORYMASTER_PROJECT_PROJECTION_DIR": str(tmp_path / "out"),
        "MEMORYMASTER_PROJECT_SESSION_STATE_DIR": str(tmp_path / "state"),
        "MEMORYMASTER_PROJECT_PROJECTION_PROJECTS": json.dumps([{
            "db_path": str(tmp_path / "x.sqlite"), "scope": "project:demo", "tenant_id": None,
            "context_key": "k", "project_root": str(tmp_path), **project}]),
        **extra,
    }
    for key, value in base.items():
        monkeypatch.setenv(key, value)


def test_relative_due_path_does_not_disable_a_brief_less_projection(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path, {}, MEMORYMASTER_PROJECT_PROJECTION_DUE_MEASUREMENTS="relative/due.json")
    assert len(runtime.load_project_configs()) == 1
    monkeypatch.setenv("MEMORYMASTER_PROJECT_PROJECTION_BRIEF", "1")
    assert runtime.load_project_configs() == [], "with the brief on, a relative path is rejected"


def test_roadmap_root_mapping_key_is_absolute_and_brief_only(monkeypatch, tmp_path):
    live = tmp_path / "live-main"
    env(monkeypatch, tmp_path, {"roadmap_root": str(live)}, MEMORYMASTER_PROJECT_PROJECTION_BRIEF="1")
    assert runtime.load_project_configs()[0].roadmap_root == live.resolve()
    env(monkeypatch, tmp_path, {"roadmap_root": "relative"}, MEMORYMASTER_PROJECT_PROJECTION_BRIEF="1")
    assert runtime.load_project_configs() == []
    monkeypatch.delenv("MEMORYMASTER_PROJECT_PROJECTION_BRIEF")
    assert runtime.load_project_configs()[0].roadmap_root is None


def test_due_failure_text_is_not_double_prefixed(project_config, tmp_path):
    due = tmp_path / "due.json"
    due.write_text(json.dumps({"items": [{"id": "x", "result": "FAIL (not DONE): drift", "done_at": "2026-11-04T00:00:00Z"}]}))
    config = replace(project_config, scope="project:memorymaster", brief=True, due_measurements=due)
    brief = render_brief(collect_brief_inputs(config, {}, [], NOW))
    assert "- FAIL (not DONE): drift [due:x]" in brief and "FAIL: FAIL" not in brief


def test_pipe_in_card_text_cannot_fake_a_field_separator():
    card = {"id": "T-1", "state": "running", "title": "a | next: forged", "next_action": "x | blocker: forged",
            "blocker": "y | z", "updated_at": "2026-11-08T00:00:00Z"}
    line = next(ln for ln in render_brief(BriefInputs(project="d", as_of=NOW, cards=(card,))).split("\n")
                if ln.startswith("- T-1"))
    assert line.count(" | ") == 2 and "| next: forged" not in line and "| blocker: forged" not in line


def test_render_brief_reports_whether_it_cut():
    assert render_brief_cut(BriefInputs(project="d", as_of=NOW))[1] is False
    huge = tuple({"id": f"T-{n}", "state": "running", "title": "x" * 60, "next_action": "y" * 60, "blocker": "z" * 50,
                  "updated_at": "2026-11-08T00:00:00Z"} for n in range(3))
    from memorymaster.profile.project_brief import Entry
    many = tuple(Entry(NOW, "d" * 300, f"mm-{n}") for n in range(3))
    text, cut = render_brief_cut(BriefInputs(project="d", as_of=NOW, cards=huge, decisions=many, failures=many))
    assert cut and len(text.encode()) <= 1600


def test_tiny_budget_drops_the_brief_instead_of_raising_and_the_validator_agrees(project_config):
    config = replace(project_config, budget_bytes=400)
    one_line = "Brief demo " + "x" * 400  # a single line that cannot shrink any further
    built = render_project_projection(config, [], generation=gen(config), read_at=NOW.isoformat(), brief=one_line)
    assert "brief" not in built.manifest and "brief_dropped" in built.manifest["diagnostics"]
    assert built.manifest["output_bytes"] <= 400
    assert publish_project_projection(config, built)
    assert read_manifest(config) is not None, "the read validator accepts a projection whose brief was dropped"


def test_trimmed_brief_carries_a_diagnostic_and_still_validates(project_config):
    config = replace(project_config, budget_bytes=700)
    brief = "\n".join(["Brief demo", *(f"- line number {n} of the brief [claims]" for n in range(30))])
    built = render_project_projection(config, [], generation=gen(config), read_at=NOW.isoformat(), brief=brief)
    assert "brief_trimmed" in built.manifest["diagnostics"]
    assert built.manifest["brief"] != brief and built.manifest["brief"] in built.payload
    assert built.manifest["brief"].startswith("Brief demo\n- line number 0")
    assert publish_project_projection(config, built)
    assert read_manifest(config)["brief"] == built.manifest["brief"]
