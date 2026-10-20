"""AO-2 v1 beat: per-run consolidation cap and run_dream config plumbing (T-1640 AC3).

The cap counts CAPTURES whose consolidation starts in one run, never single calls: a capture that
began finishes all its scopes, because a capture left half-consolidated loses the decisions of its
finished scopes and would repeat the same call every tick without ever converging.
"""

from __future__ import annotations

import runpy
from dataclasses import replace
from pathlib import Path

from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.models import DreamCandidate, ExtractionResult
from memorymaster.dreaming.worker import DreamConfig, DreamWorker

H = runpy.run_path("tests/test_dreaming_worker.py")
NOW = H["NOW"]


class _ScopedExtractor(H["_Extractor"]):
    """Same two candidates per capture, with ids unique per scope so captures never collide."""

    def extract(self, messages, *, scope, capture_hash):
        return ExtractionResult((
            DreamCandidate(f"project-c-{scope}", "The blue interface is selected for this project.", "decision", "interface", "uses", "blue", "project", "m2", "Blue interfaces", 0.8),
            DreamCandidate(f"personal-c-{scope}", "The user prefers blue interfaces.", "preference", "user", "prefers", "blue interfaces", "personal", "m1", "prefer blue interfaces", 0.9),
        ), H["_usage"]("google", self.model))


def _worker(tmp_path: Path, config: DreamConfig):
    ledger = DreamLedger(tmp_path / "capture.db")
    first = H["_capture"](ledger, scope="project:test", session_hash="a")
    second = H["_capture"](ledger, scope="project:other", session_hash="b")
    consolidator = H["_Consolidator"]()
    worker = DreamWorker(ledger, H["_service"](tmp_path), _ScopedExtractor(), consolidator, config=config, now=lambda: NOW)
    return worker, consolidator, ledger, first, second


def test_without_the_cap_one_run_consolidates_every_capture(tmp_path: Path) -> None:
    worker, consolidator, ledger, first, second = _worker(tmp_path, replace(DreamConfig(), max_consolidate_candidates=1))

    summary = worker.run(apply_candidates=False)

    assert summary["consolidated"] == 2 and len(consolidator.scopes) == 4
    assert ledger.get_capture(first)["state"] == ledger.get_capture(second)["state"] == "consolidated"


def test_the_cap_defers_whole_captures_and_every_run_makes_progress(tmp_path: Path) -> None:
    config = replace(DreamConfig(), max_consolidate_candidates=1, max_consolidate_captures_per_run=1)
    worker, consolidator, ledger, first, second = _worker(tmp_path, config)

    run_one = worker.run(apply_candidates=False)

    # One capture was started and finished across BOTH of its scopes; the other waits untouched.
    assert len(consolidator.scopes) == 2
    states = {ledger.get_capture(first)["state"], ledger.get_capture(second)["state"]}
    assert states == {"consolidated", "extracted"}
    assert run_one["consolidated"] == 1 and run_one["deferred_consolidate_budget"] == 1 and run_one["errors"] == 0

    run_two = worker.run(apply_candidates=False)

    # The second run does NOT repeat the first capture's calls: it finishes the waiting capture.
    assert len(consolidator.scopes) == 4
    assert run_two["consolidated"] == 1
    assert ledger.get_capture(first)["state"] == ledger.get_capture(second)["state"] == "consolidated"


def test_a_single_two_scope_capture_converges_in_one_run_under_the_cap(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = H["_capture"](ledger)
    consolidator = H["_Consolidator"]()
    config = replace(DreamConfig(), max_consolidate_captures_per_run=1)
    worker = DreamWorker(ledger, H["_service"](tmp_path), H["_Extractor"](), consolidator, config=config, now=lambda: NOW)

    worker.run(apply_candidates=False)
    worker.run(apply_candidates=False)

    assert len(consolidator.scopes) == 2  # personal + project, once
    assert ledger.get_capture(capture_id)["state"] == "consolidated"


class _PerCaptureExtractor(H["_Extractor"]):
    """Candidate ids unique per capture text, so several captures can share one scope."""

    def extract(self, messages, *, scope, capture_hash):
        tag = str(messages[-1]["text"])[-1]
        return ExtractionResult((
            DreamCandidate(f"project-{tag}", f"The blue interface {tag} is selected.", "decision", "interface", "uses", f"blue-{tag}", "project", "m2", "Blue interfaces", 0.8),
            DreamCandidate(f"personal-{tag}", f"The user prefers blue interfaces {tag}.", "preference", "user", "prefers", f"blue interfaces {tag}", "personal", "m1", "prefer blue interfaces", 0.9),
        ), H["_usage"]("google", self.model))


def test_captures_sharing_a_scope_cannot_bypass_the_cap_through_batch_packing(tmp_path: Path) -> None:
    """Review: with the default batch size, packing put all three captures in one call per scope."""
    ledger = DreamLedger(tmp_path / "capture.db")
    ids = [H["_capture"](ledger, session_hash=f"s{n}", extra_text=f" {n}") for n in "ABC"]
    consolidator = H["_Consolidator"]()
    config = replace(DreamConfig(), max_consolidate_captures_per_run=1)
    worker = DreamWorker(ledger, H["_service"](tmp_path), _PerCaptureExtractor(), consolidator, config=config, now=lambda: NOW)

    def states():
        return [ledger.get_capture(capture_id)["state"] for capture_id in ids]

    first = worker.run(apply_candidates=False)
    assert sorted(states()) == ["consolidated", "extracted", "extracted"]
    assert first["consolidated"] == 1 and first["deferred_consolidate_budget"] == 2 and first["errors"] == 0
    assert len(consolidator.scopes) == 2  # one capture, its personal and project scopes

    worker.run(apply_candidates=False)
    worker.run(apply_candidates=False)

    assert states() == ["consolidated"] * 3
    assert len(consolidator.scopes) == 6  # each capture consolidated exactly once, never repeated
