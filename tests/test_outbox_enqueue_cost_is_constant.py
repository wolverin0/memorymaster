"""Enqueue must stay constant-cost, asserted without a clock.

``test_sync_turn_enqueue_p95_is_below_fifty_milliseconds`` asserts p95 < 50ms.
On a shared CI runner that measures the runner, not the code: it failed at
102-148ms on windows-latest 3.11 for a commit that changed only a version
string and a changelog. Best-of-three trials was already in place and was not
enough.

Loosening that threshold is the wrong repair. At 500ms it would pass a tenfold
regression, which turns a real guarantee into a green tick that means nothing.

So assert what the hardware cannot influence: the *work* per enqueue. Every way
this operation could get slow shows up as more work — reopening the connection
per call, issuing statements proportional to the backlog, scanning pending rows.
Each is countable and deterministic on any machine.

The wall-clock test stays, retuned to a bound no healthy runner can miss, as a
backstop for a catastrophic blow-up. The precise guarantee lives here.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PLUGIN_SRC = Path(__file__).parents[1] / "integrations" / "hermes-memorymaster" / "src"
if str(PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(PLUGIN_SRC))

from hermes_memorymaster.outbox import DurableOutbox  # noqa: E402


@pytest.fixture()
def outbox(tmp_path: Path) -> DurableOutbox:
    return DurableOutbox(
        tmp_path / "outbox.db", max_pending=500, max_pending_bytes=8 * 1024 * 1024
    )


class _StatementCounter:
    """Counts SQL statements the outbox issues, via the trace callback."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.count = 0
        self._connection = connection

    def __enter__(self) -> "_StatementCounter":
        self._connection.set_trace_callback(self._record)
        return self

    def __exit__(self, *exc: object) -> None:
        self._connection.set_trace_callback(None)

    def _record(self, _statement: str) -> None:
        self.count += 1


def _enqueue(outbox: DurableOutbox, index: int) -> None:
    outbox.enqueue(f"key-{index}", {"payload": {"text": f"turn {index}"}})


def _statements_for_one_enqueue(outbox: DurableOutbox, index: int) -> int:
    with _StatementCounter(outbox._connection) as counter:
        _enqueue(outbox, index)
    return counter.count


def test_enqueue_cost_does_not_grow_with_the_backlog(outbox):
    """The regression that matters: cost proportional to pending entries."""
    first = _statements_for_one_enqueue(outbox, 0)

    for index in range(1, 200):
        _enqueue(outbox, index)

    with_backlog = _statements_for_one_enqueue(outbox, 200)

    assert with_backlog == first, (
        f"enqueue issued {first} statements on an empty outbox and "
        f"{with_backlog} with 200 pending — cost now scales with the backlog"
    )


def test_enqueue_issues_a_bounded_number_of_statements(outbox):
    """A generous ceiling that still catches an order-of-magnitude blow-up."""
    count = _statements_for_one_enqueue(outbox, 0)
    assert 0 < count <= 12, f"enqueue issued {count} SQL statements"


def test_enqueue_reuses_one_connection(outbox):
    """Reopening the database per call is the classic way this gets slow."""
    connection = outbox._connection
    for index in range(25):
        _enqueue(outbox, index)
    assert outbox._connection is connection


def test_the_counter_would_notice_extra_work(outbox):
    """Prove the instrument detects a regression, rather than trusting it.

    A test that measures nothing passes just as green as one that measures the
    right thing, so the counter itself is checked against a deliberate extra
    statement before it is relied on.
    """
    baseline = _statements_for_one_enqueue(outbox, 0)

    original_enqueue = type(outbox).enqueue

    def _wasteful(self, replay_key, envelope):
        self._connection.execute("SELECT COUNT(*) FROM outbox_entries").fetchone()
        return original_enqueue(self, replay_key, envelope)

    type(outbox).enqueue = _wasteful
    try:
        regressed = _statements_for_one_enqueue(outbox, 1)
    finally:
        type(outbox).enqueue = original_enqueue

    assert regressed > baseline, "the statement counter did not see the extra query"
