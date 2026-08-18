"""Durable, queryable provider-failure signal.

WHY THIS EXISTS (inert-signals R10). ``operational_health`` counts provider
failures and alerts above a threshold of 0. A repo-wide sweep found **no
``record_event`` anywhere that writes one**: ``llm_provider`` logged a warning
on timeout, non-zero exit and OSError and returned ``""``. Of 2,410,028
production events, 4 matched the check's pattern -- and they were ``transition``
rows. The check could not fire, so its ``provider_failures: 0`` meant "nothing
here is observable", not "nothing failed".

Two constraints shape the design:

* ``llm_provider`` holds no store or service handle, so it cannot write an
  event itself. A **sink** is registered by whoever owns a writable store
  (``stores.store_factory.create_store``), and failures are pushed to it.
* ``core.observability`` counters are module-level and in-memory
  (``_COUNTERS`` is a plain defaultdict). A health check running in another
  process -- which is the normal case, the checker is not the steward -- can
  never see them. A counter alone would have left R10 exactly as inert as it
  was, so the counter is only *half*: the durable event is what the checker
  reads.

**The durable half is throttled on purpose.** A misconfigured provider fails on
every call; writing a row each time would rebuild R9 (489,927 no-op rows, 20% of
the event log) in a new place. So at most one row per (provider, reason) per
``MEMORYMASTER_PROVIDER_FAILURE_EVENT_INTERVAL_SECONDS`` (default 300) is
written, and it carries ``occurrences`` -- the number of failures it stands for.
The first failure after a quiet period is always written immediately, so an
outage is visible within one call, not one interval.
"""
from __future__ import annotations

import logging
import os
import time
import weakref
from contextlib import contextmanager
from threading import Lock
from typing import Any, Callable, Iterator

from memorymaster.core.observability import bump_counter

logger = logging.getLogger(__name__)

# Events are recorded as `system` rather than a new event_type: EVENT_TYPES in
# core/models.py is a closed tuple mirrored by two schema files, and `system`
# already carries operational rows (491 of 2.4M in production, so a
# type-filtered scan is cheap). The details prefix is the real discriminator and
# is covered by idx_events_event_type_details.
PROVIDER_FAILURE_EVENT_TYPE = "system"
PROVIDER_FAILURE_PREFIX = "provider_failure"

DEFAULT_EVENT_INTERVAL_SECONDS = 300.0

FailureSink = Callable[[str, str, str | None, int], None]

_LOCK = Lock()
_SINK: FailureSink | None = None
# (provider, reason) -> [monotonic timestamp of last durable write, suppressed count]
_LAST_WRITE: dict[tuple[str, str], list[float]] = {}


def _label(value: str | None, default: str = "unknown") -> str:
    text = str(value or "").strip()
    return text or default


def _event_interval_seconds() -> float:
    raw = os.environ.get("MEMORYMASTER_PROVIDER_FAILURE_EVENT_INTERVAL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_EVENT_INTERVAL_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_EVENT_INTERVAL_SECONDS


def register_sink(sink: FailureSink | None) -> None:
    """Install the process-wide durable sink (``None`` clears it)."""
    global _SINK
    with _LOCK:
        _SINK = sink


def sink_registered() -> bool:
    """True when this process can write provider failures durably.

    Note what this does NOT tell you: it is a statement about the CURRENT
    process. A health check in another process learns nothing from it, which is
    why ``operational_health`` reports producer evidence from the event log
    instead.
    """
    with _LOCK:
        sink = _SINK
    if sink is None:
        return False
    store_ref = getattr(sink, "store_ref", None)
    return store_ref is None or store_ref() is not None


def reset() -> None:
    """Clear sink and throttle state (test helper / operator reset)."""
    global _SINK
    with _LOCK:
        _SINK = None
        _LAST_WRITE.clear()


def register_store_sink(store: Any) -> None:
    """Route provider failures into ``store``'s event log.

    Called from ``create_store`` so any process holding a writable store gets a
    durable producer with no wiring at the ~25 ``call_llm`` call sites. Writes
    are best-effort: a failed audit write must never turn an LLM hiccup into a
    crash.

    The store is held WEAKLY. A short-lived store (a test's temp DB, a one-shot
    CLI store) must not stay installed as the process sink after it is gone and
    silently recreate its database file on the next failure.
    """
    store_ref = weakref.ref(store)

    def _sink(provider: str, reason: str, detail: str | None, occurrences: int) -> None:
        store = store_ref()
        if store is None:
            return
        store.record_event(
            claim_id=None,
            event_type=PROVIDER_FAILURE_EVENT_TYPE,
            details=(
                f"{PROVIDER_FAILURE_PREFIX} provider={provider} "
                f"reason={reason} occurrences={occurrences}"
            ),
            payload={
                "signal": PROVIDER_FAILURE_PREFIX,
                "provider": provider,
                "reason": reason,
                "occurrences": occurrences,
                "detail": detail,
            },
        )

    # Let `sink_registered` see through to the store's liveness. A dead weakref
    # behind a "registered" sink would be this remediation's own bug: a
    # mechanism reporting that it is wired while doing nothing.
    _sink.store_ref = store_ref  # type: ignore[attr-defined]
    register_sink(_sink)


def _should_write(key: tuple[str, str], interval: float) -> int:
    """Return the occurrence count to write, or 0 to suppress this one."""
    now = time.monotonic()
    state = _LAST_WRITE.get(key)
    if state is None:
        _LAST_WRITE[key] = [now, 0]
        return 1
    last_written, suppressed = state
    if now - last_written >= interval:
        state[0] = now
        state[1] = 0
        return suppressed + 1
    state[1] = suppressed + 1
    return 0


def record_provider_failure(
    provider: str | None,
    reason: str,
    *,
    detail: str | None = None,
) -> bool:
    """Record one LLM provider failure. Returns True if a durable row was written.

    The counter is bumped on EVERY call; the event is throttled. Never raises --
    telemetry must not be able to break the call it is describing.
    """
    provider_label = _label(provider)
    reason_label = _label(reason, "unspecified")
    bump_counter("llm_provider_failures_total", provider=provider_label, reason=reason_label)

    with _LOCK:
        sink = _SINK
        if sink is None:
            return False
        occurrences = _should_write((provider_label, reason_label), _event_interval_seconds())
    if not occurrences:
        return False
    try:
        sink(provider_label, reason_label, detail, occurrences)
        return True
    except Exception:  # noqa: BLE001 - a failed audit write must not mask the failure
        logger.warning(
            "provider_health: could not record failure provider=%s reason=%s",
            provider_label,
            reason_label,
            exc_info=True,
        )
        return False


def parse_provider_failure(event: Any) -> dict[str, Any] | None:
    """Read a provider-failure event back, or None if it is not one.

    Prefers the structured payload and falls back to the details string, so rows
    written before payload parsing existed still read correctly.
    """
    if str(getattr(event, "event_type", "") or "") != PROVIDER_FAILURE_EVENT_TYPE:
        return None
    details = str(getattr(event, "details", "") or "")
    if not details.startswith(PROVIDER_FAILURE_PREFIX):
        return None

    payload = getattr(event, "payload", None)
    fields: dict[str, Any] = dict(payload) if isinstance(payload, dict) else {}
    if not fields:
        for token in details.split():
            key, _, value = token.partition("=")
            if value:
                fields[key] = value
    try:
        occurrences = max(1, int(fields.get("occurrences", 1)))
    except (TypeError, ValueError):
        occurrences = 1
    return {
        "provider": _label(fields.get("provider")),
        "reason": _label(fields.get("reason"), "unspecified"),
        "occurrences": occurrences,
    }


def configured_providers() -> list[str]:
    """Providers this process is configured to call (primary, then fallback).

    Lets the health check name which providers a `0` is claiming to be about.
    """
    providers: list[str] = []
    primary = os.environ.get("MEMORYMASTER_LLM_PROVIDER", "google").strip().lower()
    fallback = os.environ.get("MEMORYMASTER_LLM_FALLBACK_PROVIDER", "").strip().lower()
    for name in (primary, fallback):
        if name and name not in providers:
            providers.append(name)
    return providers


@contextmanager
def sink_scope(sink: FailureSink | None) -> Iterator[None]:
    """Temporarily install a sink (tests, and callers wiring a scoped store)."""
    global _SINK
    with _LOCK:
        previous = _SINK
        _SINK = sink
    try:
        yield
    finally:
        with _LOCK:
            _SINK = previous
