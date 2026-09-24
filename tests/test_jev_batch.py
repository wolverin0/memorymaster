"""The paced batch driver shared by S1 REVALIDATE and S4 DEDUP (4.9.0).

``handle`` does the store work for each answered job.  An exception there must
stop the batch cleanly with a reason -- requests already in flight are still
handled -- instead of escaping ``run_paced`` and losing the caller's summary.
"""
from __future__ import annotations

from memorymaster.govern.jev_batch import HANDLE_ERROR, run_paced


def test_a_failing_handle_stops_the_batch_with_a_reason_instead_of_raising():
    handled: list[int] = []

    def handle(job: int, answer: int) -> str | None:
        handled.append(job)
        if job == 3:
            raise RuntimeError("store write failed")
        return None

    stopped = run_paced(range(1, 50), lambda job: job * 10, handle, concurrency=4)

    assert stopped == HANDLE_ERROR
    assert 3 in handled and len(handled) < 49, "no new job is submitted after the failure"


def test_a_failing_first_job_stops_before_the_pool_starts():
    asked: list[int] = []

    def ask(job: int) -> int:
        asked.append(job)
        return job

    def handle(job: int, answer: int) -> str | None:
        raise ValueError("boom")

    assert run_paced([1, 2, 3], ask, handle, concurrency=2) == HANDLE_ERROR
    assert asked == [1]
