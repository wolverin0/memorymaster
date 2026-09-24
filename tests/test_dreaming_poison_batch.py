"""Ruling R2 (4.9.0): a poison consolidation batch cannot stall Dreaming.

A provider-wide consolidation failure opens the run's breaker and leaves its
batch untouched, so a capture whose content makes the provider fail (for
example `agy` answering ``status=ERROR``) used to trip the breaker first on
every run and stall every other scope forever (the verifier's probe).

* A batch that trips the breaker in two consecutive runs is isolated per
  capture on the next run and ordered after the other batches.
* A capture that alone trips the breaker in three consecutive runs is charged
  one error per run, bounded by ``max_capture_errors`` -> quarantine. "Alone"
  means its own isolated call failed while the provider answered another batch
  earlier in the same run; a run with no such answer (an outage) neither
  advances nor resets the streak, so outages are still never charged.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import replace

from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.worker import DreamConfig, DreamWorker

from test_dreaming_worker import (  # noqa: E402  (shared fixtures of the worker suite)
    NOW,
    _agy_consolidator,
    _agy_ignores_every_candidate,
    _agy_quota,
    _capture,
    _extracted_capture,
    _Extractor,
    _NoCall,
    _service,
)


def _agy_status_error_on(marker: str, batches: list[list[str]]):
    """A real `agy` client whose answer is ``status=ERROR`` for any batch holding ``marker``."""
    healthy = _agy_ignores_every_candidate([])

    def runner(command, payload, timeout, cwd, env):
        prompt = json.loads(payload)["message"]["content"]
        ids = json.loads(prompt.split("\n\nINPUT:\n", 1)[1])["valid_candidate_ids"]
        batches.append(sorted(ids))
        if marker in ids:
            event = {"event": "result", "result": {"status": "ERROR", "error": "internal error"}}
            return subprocess.CompletedProcess(command, 1, json.dumps(event), "")
        return healthy(command, payload, timeout, cwd, env)
    return runner


def _capture_with_personal_candidate(ledger, *, session_hash, scope, project_id, personal_id) -> int:
    """The usual extractor output: one project candidate plus one personal candidate."""
    capture_id = _capture(ledger, scope=scope, session_hash=session_hash)
    project, personal = _Extractor().extract([], scope=scope, capture_hash="a").candidates
    ledger.set_extraction(capture_id, [replace(project, candidate_id=project_id).to_dict(),
                                       replace(personal, candidate_id=personal_id).to_dict()], "fixture")
    return capture_id


def _worker(tmp_path, ledger, runner, **config):
    service = _service(tmp_path)
    consolidator = _agy_consolidator(tmp_path, runner, [])
    return DreamWorker(ledger, service, _NoCall(), consolidator, config=DreamConfig(**config), now=lambda: NOW)


def test_status_error_on_one_capture_does_not_stall_another_scope_after_two_runs(tmp_path):
    ledger = DreamLedger(tmp_path / "capture.db")
    poison = _extracted_capture(ledger, session_hash="poison", candidate_id="poison", scope="project:a")
    healthy = _extracted_capture(ledger, session_hash="healthy", candidate_id="healthy", scope="project:b")
    batches: list[list[str]] = []
    worker = _worker(tmp_path, ledger, _agy_status_error_on("poison", batches))

    for run in (1, 2):  # the poison scope sorts first and trips the breaker
        summary = worker.run(apply_candidates=False)
        assert summary["consolidate_breaker"] == "AntigravityError", run
        assert ledger.get_capture(healthy)["state"] == "extracted", run
        assert ledger.get_capture(poison)["error_count"] == 0, run
    assert batches == [["poison"], ["poison"]]
    assert ledger.consolidation_trips([poison, healthy]) == {poison: (2, 0)}  # tripped twice, never "alone"

    third = worker.run(apply_candidates=False)

    assert batches[2:] == [["healthy"], ["poison"]]  # isolated and ordered after the other batches
    assert ledger.get_capture(healthy)["state"] == "consolidated"
    assert third["consolidated"] == 1
    assert third["consolidate_isolated"] == 1
    assert ledger.get_capture(poison)["error_count"] == 0  # not charged yet


def test_packed_batch_that_trips_twice_is_split_per_capture_and_peers_complete(tmp_path):
    ledger = DreamLedger(tmp_path / "capture.db")
    poison = _extracted_capture(ledger, session_hash="poison", candidate_id="poison")
    peers = [_extracted_capture(ledger, session_hash=f"peer{i}", candidate_id=f"peer{i}") for i in range(2)]
    batches: list[list[str]] = []
    worker = _worker(tmp_path, ledger, _agy_status_error_on("poison", batches))

    for _ in range(4):
        worker.run(apply_candidates=False)

    assert batches[:2] == [["peer0", "peer1", "poison"]] * 2  # packed while it had tripped fewer than twice
    assert all(len(batch) == 1 for batch in batches[2:])  # then one call per capture
    assert [ledger.get_capture(i)["state"] for i in peers] == ["consolidated", "consolidated"]
    assert ledger.get_capture(poison)["state"] == "retryable"
    assert ledger.get_capture(poison)["error_count"] == 0  # its streak of lone trips is only 1
    assert ledger.consolidation_trips(peers) == {}  # success resets the streak


def test_capture_that_alone_trips_three_consecutive_runs_is_charged_until_quarantine(tmp_path):
    ledger = DreamLedger(tmp_path / "capture.db")
    poison = _extracted_capture(ledger, session_hash="poison", candidate_id="poison", scope="project:a")
    batches: list[list[str]] = []
    worker = _worker(tmp_path, ledger, _agy_status_error_on("poison", batches), max_capture_errors=3,
                     max_consolidate_calls_daily=100)
    errors: list[int] = []
    healthy: list[int] = []

    for run in range(1, 9):
        # Fresh healthy work every run proves the provider answers other batches.
        healthy.append(_extracted_capture(ledger, session_hash=f"h{run}", candidate_id=f"h{run}",
                                          scope="project:b"))
        worker.run(apply_candidates=False)
        errors.append(ledger.get_capture(poison)["error_count"])

    # Runs 1-2: it tripped first (no proof of health) -> isolated. Runs 3-4: it
    # alone tripped after a healthy answer (streak 1, 2). Run 5 onward: one
    # error per run, quarantined at max_capture_errors=3 and never called again.
    assert errors == [0, 0, 0, 0, 1, 2, 3, 3]
    assert ledger.get_capture(poison)["state"] == "quarantined"
    assert sum(batch == ["poison"] for batch in batches) == 7
    assert [ledger.get_capture(i)["state"] for i in healthy] == ["consolidated"] * 8


def test_outage_never_charges_isolated_captures(tmp_path):
    ledger = DreamLedger(tmp_path / "capture.db")
    ids = [_extracted_capture(ledger, session_hash=f"s{i}", candidate_id=f"c{i}", scope=scope)
           for i, scope in enumerate(["project:a", "project:a", "project:b"])]
    calls: list = []
    service = _service(tmp_path)
    outage = DreamWorker(ledger, service, _NoCall(), _agy_consolidator(tmp_path, _agy_quota, calls),
                         config=DreamConfig(max_capture_errors=1), now=lambda: NOW)

    for run in range(1, 9):
        outage.run(apply_candidates=False)
        assert [ledger.get_capture(i)["error_count"] for i in ids] == [0, 0, 0], run
        assert "quarantined" not in {ledger.get_capture(i)["state"] for i in ids}, run
    assert len(calls) == 8  # the breaker still allows only one call per run

    recovered = DreamWorker(ledger, service, _NoCall(),
                            _agy_consolidator(tmp_path, _agy_ignores_every_candidate([]), []),
                            config=DreamConfig(max_capture_errors=1), now=lambda: NOW)
    recovered.run(apply_candidates=False)
    rows = [ledger.get_capture(i) for i in ids]
    assert [row["state"] for row in rows] == ["consolidated"] * 3
    assert ledger.consolidation_trips(ids) == {}


def test_poison_capture_with_a_healthy_personal_candidate_does_not_stall_another_scope(tmp_path):
    # Verifier probe (blocking): `personal` sorts before `project:*`, so the poison
    # capture's own healthy personal batch answered first in every run and reset
    # the streak its project batch was about to extend. The streak stayed at
    # (1, 1), the capture was never isolated or charged, and project:b stalled.
    ledger = DreamLedger(tmp_path / "capture.db")
    poison = _capture_with_personal_candidate(ledger, session_hash="poison", scope="project:a",
                                              project_id="poison", personal_id="fine")
    healthy = _extracted_capture(ledger, session_hash="healthy", candidate_id="healthy", scope="project:b")
    batches: list[list[str]] = []
    worker = _worker(tmp_path, ledger, _agy_status_error_on("poison", batches), max_capture_errors=3,
                     max_consolidate_calls_daily=100)
    streaks, states, errors = [], [], []

    for _ in range(5):
        worker.run(apply_candidates=False)
        streaks.append(ledger.consolidation_trips([poison, healthy]).get(poison))
        states.append(ledger.get_capture(healthy)["state"])
        errors.append(ledger.get_capture(poison)["error_count"])

    assert batches[:4] == [["fine"], ["poison"]] * 2  # its healthy batch answers first ...
    assert streaks[:3] == [(1, 1), (2, 2), (3, 3)]  # ... and no longer resets the streak
    assert states[:3] == ["extracted", "extracted", "consolidated"]  # stalls two runs at most
    assert batches[4:7] == [["healthy"], ["fine"], ["poison"]]  # run 3: isolated, ordered last
    # It tripped alone (after an answer) from run 1: charged from run 3 on,
    # quarantined at max_capture_errors.
    assert errors == [0, 0, 1, 2, 3]
    assert ledger.get_capture(poison)["state"] == "quarantined"


def test_streak_resets_only_when_every_batch_of_the_capture_completed(tmp_path):
    # A completed call proves nothing about the capture's other batches: one
    # left behind by the breaker keeps the streak (neither advanced nor reset);
    # the run in which all its batches complete clears it.
    ledger = DreamLedger(tmp_path / "capture.db")
    split = _capture_with_personal_candidate(ledger, session_hash="split", scope="project:c",
                                             project_id="split", personal_id="fine")
    poison = _extracted_capture(ledger, session_hash="poison", candidate_id="poison", scope="project:b")
    ledger.record_consolidation_trip([split], alone=False, now=NOW)
    batches: list[list[str]] = []
    worker = _worker(tmp_path, ledger, _agy_status_error_on("poison", batches), max_consolidate_calls_daily=100)

    worker.run(apply_candidates=False)

    assert batches == [["fine"], ["poison"]]  # project:c never reached: breaker open
    assert ledger.get_capture(split)["state"] == "extracted"
    assert ledger.consolidation_trips([split]) == {split: (1, 0)}

    _worker(tmp_path, ledger, _agy_ignores_every_candidate([])).run(apply_candidates=False)

    assert ledger.get_capture(split)["state"] == "consolidated"
    assert ledger.consolidation_trips([split, poison]) == {}
