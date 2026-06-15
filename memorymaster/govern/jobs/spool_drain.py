"""Spool drainer steward phase (P1 WAL-discipline spec §2.4).

Replays spooled write envelopes (``memorymaster/spool.py``) through the
NORMAL service paths — never raw SQL:

- ``ingest`` / ``dream`` → ``svc.ingest`` — the sensitivity filter
  (``sanitize_claim_input``) and the idempotency_key/content-hash dedup
  (``service.py`` ingest) apply exactly as for a live MCP call. Per
  ``.claude/rules/sensitivity-filter.md`` every ingest path is default-deny
  until the filter is wired — it is, because we reuse ``svc.ingest``.
- ``verbatim`` → ``verbatim_store.store_verbatim`` (its own sensitivity +
  per-session dedup apply).
- ``access`` → ``store.record_accesses_batch`` — monotonic increments; a
  rare double-count on crash-replay is harmless by design.
- ``feedback`` → ``FeedbackTracker.record_retrieval``.

Idempotent on replay: a crashed drain leaves ``.draining`` files which the
next run re-claims; re-running every line is safe because ingest dedupes and
the other ops are additive. Unknown ``op``/``v`` or poison lines go to the
spool's ``quarantine/`` folder — preserved, never dropped silently, and a
single bad line can never wedge the drain.

Wired as a cycle phase at the end of ``service.run_cycle`` and exposed as the
one-shot ``drain-spool`` CLI command (the §5 rollback path: any spool residue
drains so no write is ever stranded). Reports ``{drained, quarantined,
lag_seconds}`` into the cycle result (§2.10 drain-lag metric).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from memorymaster.core import spool
from memorymaster.govern.jobs.integrity import _record
from memorymaster.core.models import CitationInput

logger = logging.getLogger(__name__)

# Drain metrics marker, recorded as a `system` event (see jobs/integrity.py).
MARKER_DRAIN = "spool_drain"

# svc.ingest kwargs a spooled payload may carry. An explicit allowlist so a
# malformed/hostile payload cannot smuggle unexpected kwargs into ingest.
_INGEST_FIELDS = (
    "claim_type",
    "subject",
    "predicate",
    "object_value",
    "scope",
    "volatility",
    "confidence",
    "event_time",
    "valid_from",
    "valid_until",
    "source_agent",
    "visibility",
    "intake_batch_id",
    "intake_batch_max",
)


def _parse_ts(raw: object) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _replay_ingest(svc, envelope: dict[str, object]) -> None:
    """Replay an ingest/dream line through svc.ingest — filter + dedup apply."""
    payload = envelope["payload"]
    kwargs = {k: payload[k] for k in _INGEST_FIELDS if payload.get(k) is not None}
    if envelope.get("op") == "dream":
        kwargs.setdefault("source_agent", "dream-bridge")
    citations = [
        CitationInput(
            source=str(c.get("source") or "spool"),
            locator=c.get("locator"),
            excerpt=c.get("excerpt"),
        )
        for c in payload.get("citations") or []
        if isinstance(c, dict)
    ]
    idempotency_key = envelope.get("idempotency_key")
    svc.ingest(
        str(payload.get("text") or ""),
        citations,
        idempotency_key=str(idempotency_key) if idempotency_key else None,
        **kwargs,
    )


def _replay_verbatim(db_path: str | Path, envelope: dict[str, object]) -> None:
    from memorymaster.recall.verbatim_store import ensure_verbatim_schema, store_verbatim

    payload = envelope["payload"]
    # verbatim_memories is historically created out-of-band by the Stop hook;
    # under the spool regime that hook never opens the DB, so the drainer must
    # be able to create the table on a fresh DB (idempotent IF NOT EXISTS).
    ensure_verbatim_schema(str(db_path))
    store_verbatim(
        str(db_path),
        session_id=str(payload.get("session_id") or ""),
        role=str(payload.get("role") or ""),
        content=str(payload.get("content") or ""),
        scope=str(payload.get("scope") or "project"),
        source_agent=str(payload.get("source_agent") or ""),
        timestamp=payload.get("timestamp"),
    )


def _replay_access(store, envelope: dict[str, object]) -> None:
    claim_ids = [int(cid) for cid in envelope["payload"].get("claim_ids") or []]
    if claim_ids:
        store.record_accesses_batch(claim_ids)


def _replay_feedback(db_path: str | Path, envelope: dict[str, object]) -> None:
    from memorymaster.govern.feedback import FeedbackTracker

    payload = envelope["payload"]
    claim_ids = [int(cid) for cid in payload.get("claim_ids") or []]
    if not claim_ids:
        return
    tracker = FeedbackTracker(str(db_path))
    tracker.record_retrieval(claim_ids, str(payload.get("query_text") or ""))


def _validate(envelope: object) -> str | None:
    """Reason string when an envelope must be quarantined, else None."""
    if not isinstance(envelope, dict):
        return "not_an_envelope"
    if envelope.get("v") != spool.SPOOL_VERSION:
        return f"unknown_version: {envelope.get('v')!r}"
    if envelope.get("op") not in spool.KNOWN_OPS:
        return f"unknown_op: {envelope.get('op')!r}"
    if not isinstance(envelope.get("payload"), dict):
        return "payload_not_a_dict"
    return None


def run(
    svc,
    *,
    db_path: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Drain the spool for this service's DB through the normal write paths.

    Never raises into the surrounding cycle; per-line failures quarantine the
    line and continue. Returns ``{drained, quarantined, files, lag_seconds,
    by_op, depth_after}``.
    """
    store = svc.store
    db_path = db_path or getattr(store, "db_path", None)
    if not db_path:
        return {"skipped": "no_sqlite_db"}

    files = spool.claim_files(db_path)
    result: dict[str, object] = {
        "drained": 0,
        "quarantined": 0,
        "files": len(files),
        "lag_seconds": 0.0,
        "by_op": {},
    }
    if not files:
        return result

    ref = now or datetime.now(timezone.utc)
    oldest: datetime | None = None
    drained = 0
    quarantined = 0
    by_op: dict[str, int] = {}
    for path in files:
        for raw in spool.read_lines(path):
            try:
                envelope = json.loads(raw)
            except json.JSONDecodeError:
                spool.quarantine_line(db_path, raw, "invalid_json")
                quarantined += 1
                continue
            reason = _validate(envelope)
            if reason is not None:
                spool.quarantine_line(db_path, raw, reason)
                quarantined += 1
                continue
            op = str(envelope["op"])
            try:
                if op in ("ingest", "dream"):
                    _replay_ingest(svc, envelope)
                elif op == "verbatim":
                    _replay_verbatim(db_path, envelope)
                elif op == "access":
                    _replay_access(store, envelope)
                else:  # "feedback" — _validate pinned op to KNOWN_OPS
                    _replay_feedback(db_path, envelope)
            except Exception as exc:
                spool.quarantine_line(db_path, raw, f"replay_error: {exc}")
                quarantined += 1
                continue
            drained += 1
            by_op[op] = by_op.get(op, 0) + 1
            stamp = _parse_ts(envelope.get("ts"))
            if stamp is not None and (oldest is None or stamp < oldest):
                oldest = stamp
        # Lines are now either replayed or quarantined — retire the file.
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("spool drain: could not remove %s: %s", path, exc)

    result["drained"] = drained
    result["quarantined"] = quarantined
    result["by_op"] = by_op
    if oldest is not None:
        result["lag_seconds"] = round(max(0.0, (ref - oldest).total_seconds()), 3)
    result["depth_after"] = spool.pending_depth(db_path)
    if quarantined:
        logger.warning(
            "spool drain: quarantined %d lines (see %s)",
            quarantined,
            spool.quarantine_dir_for(db_path),
        )
    _record(store, MARKER_DRAIN, {
        "drained": drained,
        "quarantined": quarantined,
        "files": result["files"],
        "lag_seconds": result["lag_seconds"],
        "by_op": by_op,
    })
    return result
