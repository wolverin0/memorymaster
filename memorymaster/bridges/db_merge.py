"""Bidirectional DB merge — import claims from a remote memorymaster DB.

Merges claims from a source DB into the local DB without duplicating.
Uses idempotency_key + text hash for dedup. Preserves both sides' claims.

Usage:
    memorymaster merge-db --source /path/to/remote.db
    memorymaster merge-db --source user@remote-host:/opt/memorymaster/memorymaster.db
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from memorymaster.stores._storage_shared import connect_ro, open_conn

logger = logging.getLogger(__name__)


def _text_hash(text: str) -> str:
    """Deterministic hash for claim dedup when no idempotency_key exists."""
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:16]


# Columns whose values are local to a single DB and MUST NOT be carried across
# a merge. ``human_id`` and ``idempotency_key`` are UNIQUE-indexed, so copying a
# source value can collide with an unrelated target row and (pre-fix) silently
# drop a genuinely-new claim. ``supersedes_claim_id`` / ``replaced_by_claim_id``
# reference source-side row ids that are meaningless in the target. The target
# re-allocates ``human_id`` (NULL on insert) and link references are cleared;
# conflict resolution re-establishes links using target ids afterwards.
_NON_PORTABLE_COLS = frozenset(
    {"human_id", "supersedes_claim_id", "replaced_by_claim_id"}
)


def _build_insert_values(
    row: sqlite3.Row, common_cols: list[str], ikey: str
) -> tuple[list[str], list[object]]:
    """Build column names and values for claim insertion.

    Skips non-portable columns (``human_id`` and link references) so the target
    re-allocates a fresh ``human_id`` and does not import dangling row-id links.
    """
    cols_to_insert = []
    values = []
    for col in common_cols:
        if col in _NON_PORTABLE_COLS:
            continue
        if col == "idempotency_key":
            values.append(ikey)
        else:
            values.append(row[col] if col in row.keys() else None)
        cols_to_insert.append(col)
    return cols_to_insert, values


def _copy_claim_citations(src: sqlite3.Connection, tgt: sqlite3.Connection, old_id: int, new_id: int) -> None:
    """Copy citations from source claim to target claim."""
    try:
        cites = src.execute(
            "SELECT source, locator, excerpt, created_at FROM citations WHERE claim_id = ?",
            (old_id,),
        ).fetchall()
        for cite in cites:
            tgt.execute(
                "INSERT INTO citations (claim_id, source, locator, excerpt, created_at) VALUES (?, ?, ?, ?, ?)",
                (new_id, cite["source"], cite["locator"], cite["excerpt"], cite["created_at"]),
            )
    except sqlite3.OperationalError:
        pass  # citations table might differ


def _parse_timestamp(value: object) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _citation_count(conn: sqlite3.Connection, claim_id: int) -> int:
    try:
        row = conn.execute("SELECT COUNT(*) FROM citations WHERE claim_id = ?", (claim_id,)).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0


def _claim_priority(conn: sqlite3.Connection, claim: dict[str, object]) -> tuple[bool, datetime, float, int, int]:
    claim_id = int(claim["id"])
    return (
        bool(claim.get("pinned")),
        _parse_timestamp(claim.get("updated_at")),
        float(claim.get("confidence") or 0.0),
        _citation_count(conn, claim_id),
        -claim_id,
    )


def _sync_priority(claim: dict[str, object]) -> tuple[datetime, int]:
    return (_parse_timestamp(claim.get("updated_at")), -int(claim["id"]))


def _target_columns(tgt: sqlite3.Connection) -> set[str]:
    return {col[1] for col in tgt.execute("PRAGMA table_info(claims)").fetchall()}


def _sqlite_literal_default(value: object | None) -> object | None:
    if value is None:
        return None
    raw = str(value).strip()
    if raw.upper() == "NULL":
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        quote = raw[0]
        return raw[1:-1].replace(quote * 2, quote)
    return raw


def _target_identity_defaults(tgt: sqlite3.Connection) -> dict[str, object | None]:
    identity_columns = {"tenant_id", "scope", "visibility", "source_agent"}
    defaults = {
        str(row[1]): _sqlite_literal_default(row[4])
        for row in tgt.execute("PRAGMA table_info(claims)").fetchall()
        if str(row[1]) in identity_columns
    }
    defaults.setdefault("visibility", "public")
    return defaults


IdentityNamespace = tuple[object | None, object, str, object | None]


def _identity_namespace(
    row: sqlite3.Row | dict[str, object],
    *,
    available_columns: set[str] | None = None,
    defaults: dict[str, object | None] | None = None,
) -> IdentityNamespace:
    keys = set(row.keys())
    usable = keys if available_columns is None else keys & available_columns
    defaults = defaults or {}
    tenant_id = row["tenant_id"] if "tenant_id" in usable else defaults.get("tenant_id")
    scope = row["scope"] if "scope" in usable else defaults.get("scope")
    visibility = (
        str(row["visibility"] or "public")
        if "visibility" in usable
        else str(defaults.get("visibility") or "public")
    )
    source_agent = (
        row["source_agent"]
        if "source_agent" in usable
        else defaults.get("source_agent")
    )
    return tenant_id, scope, visibility.strip().lower(), (
        source_agent if visibility.strip().lower() != "public" else None
    )


def _identity_where(
    namespace: IdentityNamespace,
    *,
    alias: str = "",
    available_columns: set[str] | None = None,
) -> tuple[str, tuple]:
    required = {"tenant_id", "scope", "visibility", "source_agent"}
    if available_columns is not None and not required.issubset(available_columns):
        return "1 = 1", ()
    prefix = f"{alias}." if alias else ""
    tenant_id, scope, visibility, source_agent = namespace
    if visibility == "public":
        return (
            f"{prefix}tenant_id IS ? AND {prefix}scope IS ? "
            f"AND {prefix}visibility = 'public'",
            (tenant_id, scope),
        )
    return (
        f"{prefix}tenant_id IS ? AND {prefix}scope IS ? "
        f"AND {prefix}visibility = ? "
        f"AND {prefix}source_agent IS ?",
        (tenant_id, scope, visibility, source_agent),
    )


def _find_conflicting_target_claims(
    tgt: sqlite3.Connection, row: sqlite3.Row, target_cols: set[str]
) -> list[dict[str, object]]:
    required = {"id", "subject", "predicate", "object_value", "scope", "status"}
    if not required.issubset(target_cols) or not required.issubset(row.keys()):
        return []
    if row["subject"] is None or row["predicate"] is None:
        return []
    identity_sql, identity_params = _identity_where(
        _identity_namespace(row, available_columns=target_cols),
        available_columns=target_cols,
    )
    conflicts = tgt.execute(
        f"""
        SELECT * FROM claims
        WHERE status != 'archived'
          AND COALESCE(subject, '') = COALESCE(?, '')
          AND COALESCE(predicate, '') = COALESCE(?, '')
          AND COALESCE(scope, '') = COALESCE(?, '')
          AND COALESCE(object_value, '') != COALESCE(?, '')
          AND {identity_sql}
        """,
        (
            row["subject"],
            row["predicate"],
            row["scope"],
            row["object_value"],
            *identity_params,
        ),
    ).fetchall()
    return [dict(conflict) for conflict in conflicts]


def _find_existing_target_claim(
    tgt: sqlite3.Connection,
    ikey: object,
    text: str,
    namespace: IdentityNamespace,
    target_cols: set[str],
    hash_to_id: dict[tuple[IdentityNamespace, str], int] | None = None,
) -> dict[str, object] | None:
    identity_sql, identity_params = _identity_where(
        namespace,
        available_columns=target_cols,
    )
    if ikey:
        row = tgt.execute(
            f"SELECT * FROM claims WHERE idempotency_key = ? AND {identity_sql}",
            (ikey, *identity_params),
        ).fetchone()
        if row:
            return dict(row)

    text_hash = _text_hash(text)
    # Indexed primary-key lookup via a precomputed {text_hash: id} map avoids the
    # O(n) full-table scan per source row (which made the merge O(n^2) overall).
    if hash_to_id is not None:
        claim_id = hash_to_id.get((namespace, text_hash))
        if claim_id is None:
            return None
        row = tgt.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
        return dict(row) if row else None

    for row in tgt.execute(
        f"SELECT * FROM claims WHERE {identity_sql}", identity_params
    ).fetchall():
        if _text_hash(row["text"]) == text_hash:
            return dict(row)
    return None


def _reconcile_existing_claim(
    tgt: sqlite3.Connection,
    source_row: sqlite3.Row,
    target_claim: dict[str, object],
    target_cols: set[str],
) -> None:
    required = {"id", "confidence", "updated_at"}
    if not required.issubset(target_cols) or not required.issubset(source_row.keys()):
        return
    source_claim = dict(source_row)
    if _sync_priority(source_claim) <= _sync_priority(target_claim):
        return
    tgt.execute(
        "UPDATE claims SET confidence = ?, updated_at = ? WHERE id = ?",
        (source_row["confidence"], source_row["updated_at"], target_claim["id"]),
    )


def _record_supersession_event(
    tgt: sqlite3.Connection, loser_id: int, winner_id: int
) -> None:
    """Append a supersession transition event for a loser claim.

    Mirrors the lifecycle invariant that every status transition leaves an
    event-chain record. Best-effort: an Atlas/legacy DB without an ``events``
    table simply skips the record rather than failing the whole merge.
    """
    try:
        now = datetime.now(timezone.utc).isoformat()
        tgt.execute(
            """
            INSERT INTO events
                (claim_id, event_type, from_status, to_status, details, created_at)
            VALUES (?, 'supersession', NULL, 'superseded', ?, ?)
            """,
            (loser_id, f"merge: superseded by claim {winner_id}", now),
        )
    except sqlite3.OperationalError:
        pass  # events table absent on this DB


def _apply_conflict_resolution(
    tgt: sqlite3.Connection,
    source_row: sqlite3.Row,
    new_id: int,
    target_cols: set[str],
    conflicts: list[dict[str, object]],
) -> None:
    if not conflicts or not {"status", "replaced_by_claim_id"}.issubset(target_cols):
        return
    merged_claim = dict(source_row)
    merged_claim["id"] = new_id
    winner = max([merged_claim, *conflicts], key=lambda claim: _claim_priority(tgt, claim))
    winner_id = int(winner["id"])
    loser_ids = [
        int(claim["id"]) for claim in [merged_claim, *conflicts] if int(claim["id"]) != winner_id
    ]
    for loser_id in loser_ids:
        # Set BOTH sides of the supersession link so the invariant holds and the
        # wiki/steward don't see a half-broken pair, and record the transition.
        tgt.execute(
            "UPDATE claims SET status = 'superseded', replaced_by_claim_id = ? WHERE id = ?",
            (winner_id, loser_id),
        )
        _record_supersession_event(tgt, loser_id, winner_id)
    # The winner's supersedes_claim_id closes the link from its side. The column
    # is single-valued; point it at the first loser it replaced.
    if loser_ids and "supersedes_claim_id" in target_cols:
        tgt.execute(
            "UPDATE claims SET supersedes_claim_id = ? WHERE id = ? AND supersedes_claim_id IS NULL",
            (loser_ids[0], winner_id),
        )


def _insert_claim_into_target(
    row: sqlite3.Row,
    common_cols: list[str],
    ikey: str,
    text: str,
    src: sqlite3.Connection,
    tgt: sqlite3.Connection,
) -> int | None:
    """Insert a single claim into target DB and copy citations. Returns new id if successful."""
    src_id = row["id"] if "id" in row.keys() else "?"
    text_hash = _text_hash(text)
    try:
        cols_to_insert, values = _build_insert_values(row, common_cols, ikey)
        placeholders = ",".join("?" for _ in cols_to_insert)
        col_names = ",".join(cols_to_insert)

        tgt.execute(
            f"INSERT INTO claims ({col_names}) VALUES ({placeholders})",
            values,
        )
        new_id = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]

        _copy_claim_citations(src, tgt, row["id"], new_id)
        return int(new_id)
    except sqlite3.IntegrityError as exc:
        # A genuine UNIQUE/constraint collision (e.g. idempotency_key already
        # present after another path inserted it). Log enough to trace which
        # claim was dropped — do NOT swallow it as a generic "merge error".
        logger.warning(
            "Constraint collision merging claim src_id=%s text_hash=%s: %s",
            src_id, text_hash, exc,
        )
        return None
    except sqlite3.OperationalError as exc:
        # Schema/operational problem (missing column, locked DB after retries).
        logger.warning(
            "Operational error merging claim src_id=%s text_hash=%s: %s",
            src_id, text_hash, exc,
        )
        return None


def _open_target(target_db: str) -> sqlite3.Connection:
    """Open the shared target DB like ``SQLiteStore.connect``.

    WAL + a busy_timeout are mandatory for the shared OpenClaw DB: without them
    a long merge transaction races concurrent readers/writers and surfaces
    sporadic ``database is locked`` errors. open_conn supplies the uniform
    envelope; the merge keeps its historical 30 s grace window (a long merge
    transaction tolerates more contention than the 15 s fleet default).
    """
    return open_conn(target_db, busy_ms=30000)


def _max_schema_version(conn: sqlite3.Connection) -> int | None:
    """Return the highest applied schema version, or None if untracked."""
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()
    except sqlite3.OperationalError:
        return None  # legacy DB without migration bookkeeping
    if row is None or row[0] is None:
        return None
    return int(row[0])


def _check_schema_compatibility(
    src: sqlite3.Connection, tgt: sqlite3.Connection
) -> None:
    """Refuse to merge across incompatible schema versions.

    If both DBs track migrations and their highest applied versions differ, a
    row valid in one may violate the other's CHECK constraints. Raise rather
    than import rows the target forbids. If either side is untracked we cannot
    compare, so we only warn.
    """
    src_v = _max_schema_version(src)
    tgt_v = _max_schema_version(tgt)
    if src_v is None or tgt_v is None:
        logger.warning(
            "Schema version unknown (source=%s, target=%s); merging without a "
            "compatibility guarantee.", src_v, tgt_v,
        )
        return
    if src_v != tgt_v:
        raise ValueError(
            "Refusing to merge across incompatible schema versions: "
            f"source applied v{src_v}, target applied v{tgt_v}. "
            "Migrate both DBs to the same version first."
        )


def merge_databases(target_db: str, source_db: str) -> dict[str, int]:
    """Merge claims from source_db into target_db.

    Skips claims that already exist (matched by idempotency_key or text hash).
    Copies citations for newly merged claims.

    Returns dict with: scanned, merged, skipped, errors
    """
    stats = {"scanned": 0, "merged": 0, "skipped": 0, "errors": 0}

    if not Path(source_db).exists():
        raise FileNotFoundError(f"Source DB not found: {source_db}")

    # Merge only reads from the source — RO mode means no lock is ever taken
    # on it (it may be another fleet's live DB). 30 s matches the old timeout.
    src = connect_ro(source_db, query_ms=30000)
    tgt = _open_target(target_db)

    try:
        # Refuse to import rows the target's CHECK constraints may forbid.
        _check_schema_compatibility(src, tgt)
        tgt_cols = _target_columns(tgt)
        identity_defaults = _target_identity_defaults(tgt)

        # Build set of existing claim fingerprints in target. The {text_hash: id}
        # map lets reconciliation look claims up by primary key instead of
        # re-scanning the whole table per source row (the old O(n^2) cost).
        existing_keys: set[tuple[IdentityNamespace, str]] = set()
        existing_hashes: set[tuple[IdentityNamespace, str]] = set()
        hash_to_id: dict[tuple[IdentityNamespace, str], int] = {}

        identity_columns = [
            column
            for column in ("tenant_id", "scope", "visibility", "source_agent")
            if column in tgt_cols
        ]
        select_columns = ", ".join(("id", "idempotency_key", "text", *identity_columns))
        for row in tgt.execute(f"SELECT {select_columns} FROM claims").fetchall():
            namespace = _identity_namespace(
                row,
                available_columns=tgt_cols,
                defaults=identity_defaults,
            )
            if row["idempotency_key"]:
                existing_keys.add((namespace, str(row["idempotency_key"])))
            thash = _text_hash(row["text"])
            existing_hashes.add((namespace, thash))
            hash_to_id.setdefault((namespace, thash), int(row["id"]))

        # Get all columns from source claims table
        src_cols = [col[1] for col in src.execute("PRAGMA table_info(claims)").fetchall()]
        # Filter to columns that exist in target
        common_cols = [c for c in src_cols if c in tgt_cols and c != "id"]

        # Scan source claims
        source_claims = src.execute("SELECT * FROM claims WHERE status != 'archived'").fetchall()

        batch_size = 200
        pending = 0
        for row in source_claims:
            stats["scanned"] += 1
            ikey = row["idempotency_key"] if "idempotency_key" in row.keys() else None
            text = row["text"]
            namespace = _identity_namespace(
                row,
                available_columns=tgt_cols,
                defaults=identity_defaults,
            )
            identity_key = (namespace, str(ikey)) if ikey else None
            hash_key = (namespace, _text_hash(text))

            # Reconcile duplicates deterministically instead of letting merge order win.
            if (identity_key and identity_key in existing_keys) or hash_key in existing_hashes:
                existing_claim = _find_existing_target_claim(
                    tgt,
                    ikey,
                    text,
                    namespace,
                    tgt_cols,
                    hash_to_id,
                )
                if existing_claim:
                    _reconcile_existing_claim(tgt, row, existing_claim, tgt_cols)
                stats["skipped"] += 1
                continue

            # Build idempotency key if missing
            if not ikey:
                ikey = f"merge-{_text_hash(text)}"

            # Insert into target
            conflicts = _find_conflicting_target_claims(tgt, row, tgt_cols)
            new_id = _insert_claim_into_target(row, common_cols, ikey, text, src, tgt)
            if new_id is not None:
                _apply_conflict_resolution(tgt, row, new_id, tgt_cols, conflicts)
                existing_keys.add((namespace, str(ikey)))
                thash = _text_hash(text)
                existing_hashes.add((namespace, thash))
                hash_to_id.setdefault((namespace, thash), new_id)
                stats["merged"] += 1
            else:
                stats["errors"] += 1

            # Commit in batches so a long single transaction doesn't hold the
            # shared DB locked for the whole merge.
            pending += 1
            if pending >= batch_size:
                tgt.commit()
                pending = 0

        tgt.commit()

    finally:
        src.close()
        tgt.close()

    logger.info(
        "Merge complete: %d scanned, %d merged, %d skipped, %d errors",
        stats["scanned"], stats["merged"], stats["skipped"], stats["errors"],
    )
    return stats
