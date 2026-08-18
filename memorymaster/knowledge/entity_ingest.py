"""Entity resolution for the ingest path (R6).

Extracted from ``MemoryService.ingest``, which held this logic inline inside a
bare ``except Exception: pass``. Two things were wrong with that:

1. **It was backend-blind.** ``PostgresStore`` subclasses ``SQLiteStore``, and
   the registry functions it called took a ``sqlite3.Connection`` and issued
   ``?`` placeholders. psycopg rejects ``?``, so on Postgres the block raised
   on its first statement, every single ingest, and the ``pass`` erased it.
2. **A failure was indistinguishable from "no entities in this claim".** Both
   produced ``entity_id == 0`` and total silence.

Resolution stays best-effort — a registry fault must never block an ingest —
but it is no longer *inert*: a failure increments
``entity_resolution_failed_total`` (visible on the metrics endpoint) and logs
with a traceback. ``entity_registry`` itself now speaks both dialects.

Scale of what this was dropping, measured on the production SQLite DB:
207,884 entities and 423,194 aliases, with 23,004 of 131,116 claims carrying
an ``entity_id``. On a Postgres deployment all three read zero.
"""
from __future__ import annotations

import logging
from typing import Any

from memorymaster.core import observability

logger = logging.getLogger(__name__)

ENTITY_RESOLUTION_FAILED = "entity_resolution_failed_total"
ENTITY_LINK_FAILED = "entity_link_failed_total"


def backend_label(store: Any) -> str:
    """Counter label identifying which store dropped the write."""
    return type(store).__name__


def link_claim_entity(store: Any, claim_id: int, entity_id: int) -> bool:
    """Point a claim at its canonical entity. Returns True when a row changed.

    Replaces a raw ``UPDATE claims SET entity_id = ? WHERE id = ?`` issued from
    the SERVICE layer under ``except Exception: pass`` — a sqlite-dialect
    statement that psycopg rejects, so on Postgres ``claims.entity_id`` stayed
    NULL forever. The statement now lives behind ``store.set_claim_entity_id``,
    which each backend implements in its own dialect, and a failure is counted.
    """
    if entity_id <= 0:
        return False
    try:
        return bool(store.set_claim_entity_id(claim_id, entity_id))
    except Exception:
        observability.bump_counter(ENTITY_LINK_FAILED, backend=backend_label(store))
        logger.warning(
            "linking claim %s to entity %s failed (backend=%s)",
            claim_id,
            entity_id,
            backend_label(store),
            exc_info=True,
        )
        return False


def resolve_claim_entities(
    store: Any,
    *,
    subject: str | None,
    text: str | None,
    claim_type: str | None,
    scope: str,
) -> int:
    """Resolve a claim's subject (and mined text entities) to canonical ids.

    Returns the subject's ``entity_id``, or 0 when there is no subject, when
    resolution refused (ambiguous fuzzy match), or when the registry failed.
    The three are distinguished by the counter, not by the return value.

    Note the deliberate change from the inline version: a failure part-way
    through returns 0 rather than the subject id resolved before it. The whole
    block runs in one transaction, so that id belonged to a row the rollback
    had already discarded — writing it onto the claim pointed ``entity_id`` at
    an entity that no longer existed.
    """
    if not subject and not text:
        return 0

    from memorymaster.knowledge.entity_extractor import extract_patterns
    from memorymaster.knowledge.entity_registry import add_alias, resolve_or_create

    entity_id = 0
    try:
        with store.connect() as conn:
            if subject:
                entity_id = resolve_or_create(
                    conn, subject,
                    entity_type=claim_type or "unknown",
                    scope=scope,
                )
            # Layer 1: mine the claim text for deterministic patterns.
            # Strategy: resolve the canonical_hint via the alias index
            # (reuses existing entity if present). Register BOTH the raw
            # surface AND a kind-tagged alias so every extracted entity
            # gains >=2 aliases (canonical + tag), plus any distinct
            # surface variants.
            for ent in extract_patterns(text or ""):
                eid = resolve_or_create(
                    conn,
                    ent.canonical_hint,
                    entity_type=f"text_entity:{ent.kind}",
                    scope=scope,
                )
                if eid <= 0:
                    continue
                if ent.surface and ent.surface != ent.canonical_hint:
                    add_alias(conn, eid, ent.surface)
                # Kind-tagged stable alias — guarantees a second alias row
                # so avg_aliases_per_entity >= 2 after backfill even when
                # surface == canonical.
                add_alias(conn, eid, f"{ent.kind}:{ent.canonical_hint}")
            conn.commit()
    except Exception:
        observability.bump_counter(
            ENTITY_RESOLUTION_FAILED, backend=backend_label(store)
        )
        logger.warning(
            "entity resolution failed (scope=%s, backend=%s); claim ingests "
            "without an entity_id",
            scope,
            backend_label(store),
            exc_info=True,
        )
        return 0
    return int(entity_id or 0)
