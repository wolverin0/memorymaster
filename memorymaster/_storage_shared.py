"""Shared constants and helper functions for the storage mixins.

Lives outside storage.py to avoid circular imports between the mixins and
the SQLiteStore class.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

HUMAN_ID_PREFIX = "mm"
EVENT_HASH_ALGO = "sha256-v1"

SQLITE_EVENTS_APPEND_ONLY_TRIGGERS = (
    "trg_events_append_only_update",
    "trg_events_append_only_delete",
)
SQLITE_CONFIRMED_TUPLE_GUARD_TRIGGERS = (
    "trg_claims_confirmed_tuple_guard_insert",
    "trg_claims_confirmed_tuple_guard_update",
)


def generate_human_id_hash(text: str) -> str:
    """Generate a 4-hex-char hash from text for human-readable IDs."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return digest[:4]


def generate_top_level_human_id(subject: str | None, text: str) -> str:
    """Generate a top-level human_id like ``mm-a3f8``."""
    seed = (subject or text).strip()
    return f"{HUMAN_ID_PREFIX}-{generate_human_id_hash(seed)}"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ConcurrentModificationError(RuntimeError):
    """Raised when an optimistic-lock check fails during a status transition."""
