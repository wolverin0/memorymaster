"""Read-only archive gate: has S1 judged a stale claim no longer useful?

Scheduled archival is irreversible (``archived`` is terminal), so it may only
act on a recorded judgment, never on the clock alone (review F-20, contract
``.planning/JEV-LIVE-4.9.0.md``). This module reads the decisions ledger
sidecar and never writes, creates or migrates it.

Judgment convention (what ``decisions.engine.decide`` writes for S1):

* ledger: ``MEMORYMASTER_DECISIONS_DB`` (default ``~/.memorymaster/decisions.db``),
  opened ``mode=ro``; a missing file, table or column means "no judgment";
* a judgment is a ``decisions`` row with surface ``revalidate`` and mode
  ``live`` (both compared case-insensitively; the engine stores them
  lowercased), no ``fallback_reason`` and an ``action_taken`` of
  ``no_longer_useful`` -- JSON-encoded as the engine stores it
  (``'"no_longer_useful"'``) or raw -- linked through
  ``decision_items.item_ref`` = the claim id (``"123"`` or ``"claim:123"``);
* the most recent live, non-fallback REVALIDATE decision for the claim wins,
  so a later different verdict revokes an earlier ``no_longer_useful``;
* ``not_before`` (ISO-8601, typically the claim's ``updated_at``) rejects a
  judgment about an earlier state of the claim.

Shadow-mode or fallback decisions never authorize archival: in those modes
``action_taken`` is the legacy action, not Jev's verdict.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger(__name__)

ENV_DECISIONS_DB = "MEMORYMASTER_DECISIONS_DB"
SURFACE = "revalidate"
JUDGMENT = "no_longer_useful"
_CHUNK = 400


def ledger_path() -> Path:
    configured = os.environ.get(ENV_DECISIONS_DB, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".memorymaster" / "decisions.db"


def ledger_available(db_path: str | Path | None = None) -> bool:
    """True when the ledger file exists; never creates it."""
    return (Path(db_path) if db_path is not None else ledger_path()).is_file()


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _action(value: object) -> str:
    """``action_taken`` as a plain string: the engine JSON-encodes it."""
    text = str(value or "").strip()
    try:
        decoded = json.loads(text)
    except ValueError:
        return text
    return decoded.strip() if isinstance(decoded, str) else text


def _claim_id(item_ref: object) -> int | None:
    text = str(item_ref or "").strip()
    if text.startswith("claim:"):
        text = text[len("claim:"):]
    return int(text) if text.isdigit() else None


def judged_no_longer_useful(
    not_before_by_claim: Mapping[int, str | None] | Iterable[int],
    *,
    db_path: str | Path | None = None,
) -> set[int]:
    """Return the claim ids whose latest live S1 verdict is ``no_longer_useful``.

    Accepts claim ids, or a mapping of claim id -> ``not_before`` timestamp.
    Any ledger problem yields an empty set (fail closed: nothing archives).
    """
    bounds: dict[int, str | None] = (
        dict(not_before_by_claim) if isinstance(not_before_by_claim, Mapping)
        else {int(claim_id): None for claim_id in not_before_by_claim}
    )
    path = Path(db_path) if db_path is not None else ledger_path()
    if not bounds or not ledger_available(path):
        return set()
    latest: dict[int, tuple[datetime, str, str]] = {}
    try:
        conn = sqlite3.connect(f"file:{quote(path.as_posix(), safe='/:')}?mode=ro", uri=True, timeout=5)
        try:
            conn.execute("PRAGMA query_only=ON")
            ids = list(bounds)
            for start in range(0, len(ids), _CHUNK):
                chunk = ids[start:start + _CHUNK]
                refs = [str(i) for i in chunk] + [f"claim:{i}" for i in chunk]
                rows = conn.execute(
                    "SELECT DISTINCT i.item_ref, d.ts, d.decision_id, d.action_taken "
                    "FROM decisions d JOIN decision_items i ON i.decision_id = d.decision_id "
                    "WHERE lower(d.surface) = ? AND lower(d.mode) = 'live' "
                    "AND COALESCE(d.fallback_reason, '') = '' "
                    f"AND i.item_ref IN ({','.join('?' for _ in refs)})",
                    [SURFACE, *refs],
                ).fetchall()
                for item_ref, ts, decision_id, action in rows:
                    claim_id, stamp = _claim_id(item_ref), _parse_ts(ts)
                    if claim_id is None or stamp is None:
                        continue
                    key = (stamp, str(decision_id), _action(action))
                    if claim_id not in latest or key[:2] > latest[claim_id][:2]:
                        latest[claim_id] = key
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.debug("decisions ledger unreadable for the archive gate: %s", exc)
        return set()
    judged: set[int] = set()
    for claim_id, (stamp, _decision_id, action) in latest.items():
        if action != JUDGMENT:
            continue
        floor = _parse_ts(bounds.get(claim_id))
        if floor is not None and stamp < floor:
            continue
        judged.add(claim_id)
    return judged


def has_no_longer_useful_judgment(
    claim_id: int,
    *,
    not_before: str | None = None,
    db_path: str | Path | None = None,
) -> bool:
    """True only when the ledger records a live S1 ``no_longer_useful`` verdict."""
    return int(claim_id) in judged_no_longer_useful({int(claim_id): not_before}, db_path=db_path)


__all__ = [
    "ENV_DECISIONS_DB",
    "JUDGMENT",
    "SURFACE",
    "has_no_longer_useful_judgment",
    "judged_no_longer_useful",
    "ledger_available",
    "ledger_path",
]
