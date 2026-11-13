"""T-2123: a capture's messages are sanitized once, however many candidates bind to it, and the lease is held."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from memorymaster.dreaming import source_review
from memorymaster.dreaming.models import DreamCandidate
from memorymaster.dreaming.worker import DreamConfig, DreamWorker

MESSAGES = 3000
CANDIDATES = 100


def _row() -> dict:
    messages = [{"message_id": f"m{i}", "role": "user" if i % 2 else "assistant",
                 "text": f"note {i} about it" + (f" key sk-ant-api03-{'a' * 40} /home/someone/x" if i % 100 == 0 else ""),
                 "timestamp": "t"}
                for i in range(MESSAGES)]
    return {"id": 1, "scope": "project:test", "provider": "p", "session_hash": "s", "messages": messages,
            "decisions": None, "released": [
                DreamCandidate(f"c{n}", "a lesson", "lesson", "s", "p", "o", "project",
                               f"m{n * 29}", f"note {n * 29} about", .5).to_dict() for n in range(CANDIDATES)]}


class _Ledger:
    renewals = 0

    def renew_lease(self, *_args, **_kwargs) -> bool:
        self.renewals += 1
        return True


def _inputs(row: dict, ledger: _Ledger):
    worker = DreamWorker(ledger, None, None, None, config=DreamConfig(),
                         now=lambda: datetime(2026, 11, 11, tzinfo=timezone.utc))
    worker._lease_owner = "owner"
    return worker._consolidation_inputs("run", [row], {"consolidated": 0})


def test_messages_are_sanitized_once_per_capture_and_lease_renewed_while_binding(monkeypatch) -> None:
    calls = {"messages": 0}
    real = source_review.safe_context

    def spy(value):
        if isinstance(value, list) and value and isinstance(value[0], dict) and "message_id" in value[0]:
            calls["messages"] += 1
        return real(value)

    monkeypatch.setattr(source_review, "safe_context", spy)
    ledger = _Ledger()
    started = time.perf_counter()
    _rows, groups = _inputs(_row(), ledger)
    elapsed = time.perf_counter() - started
    bound = groups["project:test"]
    assert len(bound) == CANDIDATES
    assert calls["messages"] == 1
    assert ledger.renewals >= CANDIDATES
    assert elapsed < 5.0  # coarse guard; the call count above is the exact check
    # Everything that reaches the provider is still sanitized.
    assert all("sk-ant-api03" not in str(c.source_context) and "/home/someone" not in str(c.source_context)
               for _row_, c in bound)


def test_precomputed_safe_messages_bind_identically_to_the_per_call_path() -> None:
    row = _row()
    row["messages"] = row["messages"][:2000]  # large enough that the 24,000-char window cut is exercised
    cut = DreamCandidate("c", "a lesson", "lesson", "s", "p", "o", "project", "m30", "note 30 about", .5)
    prepared = source_review.sanitized_messages(row)
    assert prepared == source_review.safe_context(source_review._context_messages(row))
    before = source_review.bind_source(cut, row)
    after = source_review.bind_source(cut, row, safe_messages=prepared)
    assert before == after
    assert before.source_context["complete"] is False or "omitted_before" in before.source_context
    assert len(prepared) == 2000  # the shared list is never trimmed by a bind
