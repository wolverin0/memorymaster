"""AO-2 v1 beat: per-run consolidation cap and run_dream config plumbing (T-1640 AC3)."""

from __future__ import annotations

import runpy
from dataclasses import replace
from pathlib import Path

from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.worker import DreamConfig, DreamWorker

H = runpy.run_path("tests/test_dreaming_worker.py")
NOW = H["NOW"]


def _run(tmp_path: Path, config: DreamConfig):
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = H["_capture"](ledger)
    consolidator = H["_Consolidator"]()
    worker = DreamWorker(ledger, H["_service"](tmp_path), H["_Extractor"](), consolidator, config=config, now=lambda: NOW)
    return worker.run(apply_candidates=False), consolidator, ledger, capture_id


def test_without_the_cap_both_scopes_are_consolidated_in_one_run(tmp_path: Path) -> None:
    summary, consolidator, _, _ = _run(tmp_path, DreamConfig())

    assert len(consolidator.scopes) == 2 and summary["consolidated"] == 1


def test_per_run_cap_of_one_makes_a_single_consolidation_call_and_defers_the_rest(tmp_path: Path) -> None:
    summary, consolidator, ledger, capture_id = _run(tmp_path, replace(DreamConfig(), max_consolidate_calls_per_run=1))

    assert len(consolidator.scopes) == 1
    assert summary["deferred_consolidate_budget"] == 1 and summary["errors"] == 0
    row = ledger.get_capture(capture_id)
    assert row["state"] == "extracted" and row["deferred_reason"] == "consolidate_budget"
    # The extraction is kept: the next run resumes at consolidation, not at the extractor.
    assert row["extraction"]
