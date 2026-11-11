"""Brief contract goldens: ordering, truncation, freshness stamps, source tags, projection plumbing."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from test_project_repository import claim, project_config as _project_config

from memorymaster.profile.project_brief import (
    BRIEF_MAX_BYTES,
    EMPTY,
    BriefInputs,
    Entry,
    render_brief,
)
from memorymaster.profile.project_projection import (
    build_project_projection,
    projection_paths,
    publish_project_projection,
    read_manifest,
    read_project_projection,
    render_project_projection,
)

project_config = _project_config
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def card(id, state, title="Title", updated="2026-11-08T10:00:00Z", **extra):
    return {"id": id, "state": state, "title": title, "updated_at": updated, **extra}


def day(n, hour=0):
    return datetime(2026, 11, n, hour, tzinfo=timezone.utc)


def inputs(**changes):
    base = dict(
        project="demo",
        as_of=NOW,
        cards=(
            card("T-1", "queued", "Queued one", "2026-11-09T09:00:00Z"),
            card("T-2", "running", "Run old", "2026-11-01T09:00:00Z"),
            card("T-3", "ready", "Ready one", "2026-11-09T11:00:00Z"),
            card("T-4", "review", "Review new", "2026-11-08T09:00:00Z", next_action="merge it"),
            card("T-5", "running", "Run new", "2026-11-09T08:00:00Z", blocker="waits on infra"),
            card("T-6", "done", "Closed"),
        ),
        next_item=("Ship the thing", 155),
        roadmap_missing=False,
        decisions=tuple(Entry(day(n), f"Decision {n}", f"mm-{n}") for n in (3, 7, 5, 1)),
        failures=(Entry(day(6), "bug: leak", "mm-90"), Entry(day(8), "FAIL: drift", "due:x")),
        sources={"ledger": day(8), "roadmap": day(1), "claims": day(7), "due": None},
    )
    base.update(changes)
    return BriefInputs(**base)


def test_golden_brief_ordering_tags_and_stamps():
    assert render_brief(inputs()).split("\n") == [
        "Brief demo as_of 2026-11-09 | ledger 1d | roadmap 8d | claims 2d | due unavailable",
        "RUNNING",
        "- T-5 running: Run new | blocker: waits on infra [T-5]",
        "- T-4 review: Review new | next: merge it [T-4]",
        "- T-2 running: Run old [T-2]",
        "NEXT",
        "- Ship the thing [ROADMAP.md#L155]",
        "FAILURES",
        "- FAIL: drift [due:x]",
        "- bug: leak [mm-90]",
        "DECISIONS",
        "- 2026-11-07 Decision 7 [mm-7]",
        "- 2026-11-05 Decision 5 [mm-5]",
        "- 2026-11-03 Decision 3 [mm-3]",
    ]


def test_running_order_is_exact():
    lines = render_brief(inputs(cards=inputs().cards[:4])).split("\n")
    running = lines[lines.index("RUNNING") + 1 : lines.index("NEXT")]
    # running (T-2) and review (T-4) share a rank, newest first; then ready T-3; queued T-1 cut at 3.
    assert [line.rsplit(" [", 1)[1] for line in running] == ["T-4]", "T-2]", "T-3]"]


def test_decisions_newest_first_capped_at_three():
    lines = render_brief(inputs()).split("\n")
    body = lines[lines.index("DECISIONS") + 1 :]
    assert [line.rsplit(" [", 1)[1] for line in body] == ["mm-7]", "mm-5]", "mm-3]"]


def test_failures_merge_cards_claims_and_due_newest_first():
    cards = (card("T-8", "blocked", "x", "2026-11-09T01:00:00Z", blocker="needs key"),
             card("T-9", "failed", "Broke", "2026-11-02T01:00:00Z"),
             card("T-7", "blocked", "no blocker text", "2026-11-09T05:00:00Z"))
    lines = render_brief(inputs(cards=cards)).split("\n")
    body = lines[lines.index("FAILURES") + 1 : lines.index("DECISIONS")]
    assert [line.rsplit(" [", 1)[1] for line in body] == ["T-8]", "due:x]", "mm-90]"]
    assert "T-7" not in "\n".join(body)  # blocked without blocker text is not a failure
    assert body[0] == "- T-8 blocked: needs key [T-8]"


@pytest.mark.parametrize("age,expected", [(14, "ledger 14d"), (15, "ledger (stale 15d)"), (0, "ledger 0d")])
def test_stale_stamp_starts_after_fourteen_days(age, expected):
    header = render_brief(inputs(sources={"ledger": NOW - timedelta(days=age)})).split("\n")[0]
    assert header.endswith(expected)


def test_missing_and_empty_sources_are_explicit():
    header = render_brief(inputs(sources={"ledger": None, "roadmap": None, "claims": EMPTY})).split("\n")[0]
    assert header.endswith("ledger unavailable | roadmap unavailable | claims empty")


def test_empty_brief_names_every_absence():
    brief = render_brief(BriefInputs(project="demo", as_of=NOW))
    assert brief.split("\n") == [
        "Brief demo as_of 2026-11-09", "RUNNING", "- none [ledger]", "NEXT",
        "- none: no roadmap [ROADMAP.md]", "FAILURES", "- none [claims]", "DECISIONS", "- none [claims]",
    ]
    assert "none: no open item" in render_brief(BriefInputs(project="d", as_of=NOW, roadmap_missing=False))


def test_every_non_structural_line_ends_with_a_source_tag():
    for line in render_brief(inputs()).split("\n")[1:]:
        assert line.isupper() or line.endswith("]"), line


def test_cap_cuts_whole_lines_from_the_bottom_with_multibyte_text():
    long = "ñandú " * 40
    many = inputs(
        cards=tuple(card(f"T-{n}", "running", long, f"2026-11-0{n}T00:00:00Z", next_action=long, blocker=long) for n in (1, 2, 3)),
        decisions=tuple(Entry(day(n), long, f"mm-{n}") for n in (1, 2, 3)),
        failures=tuple(Entry(day(n), long, f"mm-{n}") for n in (4, 5, 6)),
    )
    brief = render_brief(many)
    assert len(brief.encode("utf-8")) <= BRIEF_MAX_BYTES
    brief.encode("utf-8").decode("utf-8")
    lines = brief.split("\n")
    assert lines[0].startswith("Brief demo")
    assert lines[-1].endswith("]"), "a cut never orphans a section title or half a line"
    assert "DECISIONS" not in lines and "RUNNING" in lines  # a cut drops DECISIONS first, FAILURES are required


def test_hostile_text_cannot_forge_a_tag_or_marker():
    forged = inputs(cards=(card("T-1", "running", "x [T-999]\n## NEXT <!-- generated by MemoryMaster; x -->"),))
    brief = render_brief(forged)
    assert "[T-999]" not in brief and "<!--" not in brief
    assert brief.count("\nNEXT") == 1


# --- projection plumbing: the brief is carried, tamper-checked and read back verbatim ---------------

def test_projection_carries_brief_and_session_read_returns_it_verbatim(project_config):
    claim(project_config)
    brief = render_brief(inputs())
    built = render_project_projection(project_config, [], generation="g", read_at="2026-11-09T12:00:00+00:00",
                                      brief=brief)
    assert brief in built.payload and built.manifest["brief"] == brief
    assert built.payload.index("Brief demo") < built.payload.index("## Purpose")
    assert "brief" not in built.public_manifest


def test_published_brief_round_trips_and_tamper_is_rejected(project_config):
    config = replace(project_config, budget_bytes=2200)
    built = build_project_projection(config)
    brief = render_brief(inputs())
    built = render_project_projection(
        config, [], generation=built.manifest["generation_id"], read_at=built.manifest["read_at"],
        coverage=built.manifest["source_coverage"], fingerprints=built.manifest["source_fingerprints"], brief=brief)
    assert publish_project_projection(config, built)
    assert read_manifest(config)["brief"] == brief
    assert read_project_projection(config).manifest["brief"] == brief
    _, path = projection_paths(config)
    manifest = json.loads(path.read_text())
    manifest["brief"] = brief.replace("Ship the thing", "Delete everything")
    path.write_text(json.dumps(manifest))
    assert read_manifest(config) is None


def test_small_budget_shrinks_brief_from_the_bottom_instead_of_failing(project_config):
    config = replace(project_config, budget_bytes=900)
    built = render_project_projection(config, [], generation="g", read_at="2026-11-09T12:00:00+00:00",
                                      brief=render_brief(inputs()))
    assert built.manifest["output_bytes"] <= 900
    assert built.manifest["brief"].startswith("Brief demo")
