"""The shipped steward-cycle hook wires S1 and the outcome joiners in contract order.

S1 REVALIDATE runs after ``run_cycle`` (so decay has produced this cycle's stale
claims) and before ``scheduled_archive`` (so a fresh ``no_longer_useful``
judgment can gate archival), capped per cycle by
``MEMORYMASTER_JEV_REVALIDATE_PER_CYCLE``.  The lifecycle tail runs once per
cycle after every stage that writes lifecycle events; retention prune is daily.
"""
from __future__ import annotations

import ast
from pathlib import Path

TEMPLATE = (Path(__file__).resolve().parents[1] / "memorymaster" / "config_templates" / "hooks"
            / "memorymaster-steward-cycle.py")


def _source() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _position(source: str, needle: str) -> int:
    index = source.find(needle)
    assert index >= 0, f"{needle!r} missing from the steward-cycle template"
    return index


def test_revalidation_runs_after_the_cycle_and_before_scheduled_archive():
    source = _source()
    cycle = _position(source, "svc.run_cycle(")
    revalidate = _position(source, "revalidation.run(")
    archive = _position(source, "scheduled_archive.run(")
    assert cycle < revalidate < archive


def test_revalidation_is_capped_per_cycle_from_the_environment():
    source = _source()
    assert "revalidation.run(_svc, limit=revalidation.per_cycle_limit())" in source


def test_s4_dedup_runs_only_from_the_hook_after_the_cycle_with_its_own_cap():
    source = _source()
    cycle = _position(source, "svc.run_cycle(")
    dedup = _position(source, "candidate_dedupe.run_jev(")
    assert cycle < dedup < _position(source, "steward_cycle_outcomes(DB_PATH)")
    assert "candidate_dedupe.run_jev(_svc.store, limit=candidate_dedupe.jev_pairs_per_cycle())" in source


def test_lifecycle_outcomes_join_once_after_the_writing_stages():
    source = _source()
    joiner = _position(source, "steward_cycle_outcomes(DB_PATH)")
    assert source.count("steward_cycle_outcomes(") == 1
    assert joiner > _position(source, "curation_drain.run(")
    assert joiner > _position(source, "scheduled_archive.run(")


def test_each_new_stage_is_failure_isolated():
    tree = ast.parse(_source())
    guarded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            text = ast.unparse(node)
            for name in ("revalidation.run(", "candidate_dedupe.run_jev(", "steward_cycle_outcomes("):
                if name in text and node.handlers:
                    guarded.add(name)
    assert guarded == {"revalidation.run(", "candidate_dedupe.run_jev(", "steward_cycle_outcomes("}
