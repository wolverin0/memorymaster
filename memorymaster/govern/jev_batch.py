"""Bounded, paced driver for the batch Jev surfaces (S1 revalidation, S4 dedup).

Only the network wait runs in worker threads: ``ask`` calls ``engine.decide``
(which never raises and does its own egress, budget, breaker and ledger work),
while ``handle`` -- every read or write of the authoritative store -- runs in the
caller's thread, one decision at a time.  The first job runs alone so the
engine's lazy transport and question registration are set up before any
concurrency.  Submission is paced below the engine's RPM cap so a batch does not
starve the interactive hooks that share the ledger-wide budget.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any, Callable, Iterable, Iterator, TypeVar

T = TypeVar("T")
logger = logging.getLogger(__name__)
#: Stop reason when ``handle`` (the store work for an answer) raises.
HANDLE_ERROR = "handle_error"

#: Fallback reasons after which a batch stops asking: sending more would spend
#: money the budget forbids, or keep hitting a surface the breaker has opened.
STOP_REASONS = frozenset({"budget_exhausted", "ledger_unavailable", "missing_key", "breaker_open"})


APPLY_FAILED = "apply_failed"


def record_apply_failed(engine: Any, decision_id: str, item_ref: str, action: str, reason: str) -> None:
    """Append an ``apply_failed`` outcome: the decision row says ``action`` but the store refused it.

    Off-policy evaluation and RL read ``action_taken``; without this row they
    would count an action that never happened (claim changed, proposal already
    filed, promotions frozen, ...).  Ledger writes never raise.
    """
    from memorymaster.decisions.outcomes import record_outcome

    record_outcome(engine.ledger, decision_id, item_ref, APPLY_FAILED, value=0.0, label_source="surface",
                   details={"action": action, "reason": reason})


class Pacer:
    """Token bucket at 80 % of ``rpm_cap`` with a burst of 10 %: at most 90 % per minute."""

    def __init__(self, rpm_cap: int, *, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        cap = max(int(rpm_cap), 1)
        self.rate = max(0.8 * cap, 1.0) / 60.0
        self.capacity = max(0.1 * cap, 1.0)
        self.tokens = self.capacity
        self._clock = monotonic
        self._sleep = sleep
        self._last = monotonic()

    def wait(self) -> None:
        now = self._clock()
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
        self._last = now
        if self.tokens < 1.0:
            delay = (1.0 - self.tokens) / self.rate
            self._sleep(delay)
            self._last = self._clock()
            self.tokens = 1.0
        self.tokens -= 1.0


def run_paced(jobs: Iterable[T], ask: Callable[[T], Any], handle: Callable[[T, Any], str | None], *,
              concurrency: int = 8, pacer: Pacer | None = None) -> str | None:
    """Ask for every job with at most ``concurrency`` requests in flight.

    ``handle(job, answer)`` returns a stop reason to stop submitting; requests
    already in flight still complete and are handled.  An ``ask`` that raises is
    handled with ``None``; a ``handle`` that raises stops the batch with
    ``handle_error`` instead of escaping (the caller keeps its summary).  Returns
    the first stop reason, or ``None`` when the jobs ran out.
    """
    def safe_ask(job: T) -> Any:
        try:
            return ask(job)
        except Exception:  # noqa: BLE001 - one bad job must not end the batch
            return None

    def safe_handle(job: T, answer: Any) -> str | None:
        try:
            return handle(job, answer)
        except Exception as exc:  # noqa: BLE001 - a store fault stops the batch, it does not escape
            logger.warning("Jev batch stopped: acting on an answer failed (%s)", type(exc).__name__)
            return HANDLE_ERROR

    iterator: Iterator[T] = iter(jobs)
    first = next(iterator, None)
    if first is None:
        return None
    if pacer is not None:
        pacer.wait()
    stopped = safe_handle(first, safe_ask(first))
    workers = max(int(concurrency), 1)
    if stopped is not None:
        return stopped
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="jev-batch") as pool:
        pending: dict[Future, T] = {}
        exhausted = False
        while True:
            while stopped is None and not exhausted and len(pending) < workers:
                job = next(iterator, None)
                if job is None:
                    exhausted = True
                    break
                if pacer is not None:
                    pacer.wait()
                pending[pool.submit(safe_ask, job)] = job
            if not pending:
                return stopped
            done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
            for future in done:
                job = pending.pop(future)
                reason = safe_handle(job, future.result())
                if stopped is None and reason is not None:
                    stopped = reason


__all__ = ["APPLY_FAILED", "HANDLE_ERROR", "Pacer", "STOP_REASONS", "record_apply_failed", "run_paced"]
