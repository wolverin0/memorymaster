"""Claim lifecycle, events, embeddings, links, access tracking for SQLiteStore.

This is a mixin class for memorymaster.storage.SQLiteStore. All methods
expect to be bound to a SQLiteStore instance and rely on `self.connect()`
and `self.db_path`. Do not instantiate directly.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memorymaster.embeddings import EmbeddingProvider, cosine_similarity
from memorymaster.models import (
    CLAIM_LINK_TYPES,
    CLAIM_STATUSES,
    STATUS_TRANSITION_EVENT_TYPES,
    Citation,
    CitationInput,
    Claim,
    ClaimLink,
    Event,
    validate_event_payload,
    validate_event_type,
    validate_transition_event_type,
)

logger = logging.getLogger(__name__)

from memorymaster._storage_shared import (
    EVENT_HASH_ALGO,
    HUMAN_ID_PREFIX,
    SQLITE_CONFIRMED_TUPLE_GUARD_TRIGGERS,
    SQLITE_EVENTS_APPEND_ONLY_TRIGGERS,
    ConcurrentModificationError,
    generate_human_id_hash,
    generate_top_level_human_id,
    utc_now,
)


class _LifecycleMixin:

    def apply_status_transition(
        self,
        claim: Claim,
        *,
        to_status: str,
        reason: str,
        event_type: str,
        replaced_by_claim_id: int | None = None,
    ) -> Claim:
        validated_event_type = validate_transition_event_type(event_type)
        now = utc_now()
        last_validated_at = now if to_status in {"confirmed", "stale", "conflicted"} else claim.last_validated_at
        archived_at = now if to_status == "archived" else None
        next_replaced_by = replaced_by_claim_id if replaced_by_claim_id is not None else claim.replaced_by_claim_id

        # Set valid_until when superseding — the old claim is no longer current truth
        valid_until_update = now if to_status == "superseded" else None

        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE claims
                SET status = ?, updated_at = ?, last_validated_at = ?, archived_at = ?,
                    replaced_by_claim_id = ?, version = version + 1,
                    valid_until = COALESCE(?, valid_until)
                WHERE id = ? AND version = ?
                """,
                (to_status, now, last_validated_at, archived_at, next_replaced_by, valid_until_update, claim.id, claim.version),
            )
            if cur.rowcount == 0:
                raise ConcurrentModificationError(
                    f"Claim {claim.id} was modified by another writer (version mismatch). Reload and retry."
                )
            self._insert_event_row(
                conn,
                claim_id=claim.id,
                event_type=validated_event_type,
                from_status=claim.status,
                to_status=to_status,
                details=reason,
                payload_json=json.dumps({"replaced_by_claim_id": replaced_by_claim_id}) if replaced_by_claim_id else None,
                created_at=now,
            )
            conn.commit()
        updated = self.get_claim(claim.id)
        if updated is None:
            raise RuntimeError("Failed to load claim after transition.")
        return updated


    def set_supersedes(self, claim_id: int, supersedes_claim_id: int) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE claims
                SET supersedes_claim_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (supersedes_claim_id, now, claim_id),
            )
            conn.commit()


    def mark_superseded(self, old_claim_id: int, new_claim_id: int, reason: str) -> None:
        old_claim = self.get_claim(old_claim_id, include_citations=False)
        if old_claim is None:
            return
        self.apply_status_transition(
            old_claim,
            to_status="superseded",
            reason=reason,
            event_type="supersession",
            replaced_by_claim_id=new_claim_id,
        )
        self.set_supersedes(new_claim_id, old_claim_id)


    def delete_old_events(self, retain_days: int) -> int:
        # Events are append-only by contract; retention trim is a no-op.
        return 0


    def reconcile_integrity(self, *, fix: bool = False, limit: int = 500) -> dict[str, object]:
        report: dict[str, object] = {
            "checked_at": utc_now(),
            "fix_mode": bool(fix),
            "issues": {},
            "actions": [],
        }
        with self.connect() as conn:
            self._ensure_event_integrity_schema(conn)

            orphan_events = conn.execute(
                """
                SELECT e.id
                FROM events e
                LEFT JOIN claims c ON c.id = e.claim_id
                WHERE e.claim_id IS NOT NULL AND c.id IS NULL
                ORDER BY e.id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            orphan_citations = conn.execute(
                """
                SELECT ci.id
                FROM citations ci
                LEFT JOIN claims c ON c.id = ci.claim_id
                WHERE c.id IS NULL
                ORDER BY ci.id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            superseded_without_replacement = conn.execute(
                """
                SELECT id
                FROM claims
                WHERE status = 'superseded' AND replaced_by_claim_id IS NULL
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            dangling_replaced_by = conn.execute(
                """
                SELECT c.id
                FROM claims c
                LEFT JOIN claims n ON n.id = c.replaced_by_claim_id
                WHERE c.replaced_by_claim_id IS NOT NULL AND n.id IS NULL
                ORDER BY c.id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            dangling_supersedes = conn.execute(
                """
                SELECT c.id
                FROM claims c
                LEFT JOIN claims p ON p.id = c.supersedes_claim_id
                WHERE c.supersedes_claim_id IS NOT NULL AND p.id IS NULL
                ORDER BY c.id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

            transition_issues: list[dict[str, object]] = []
            transition_placeholders = ",".join("?" for _ in STATUS_TRANSITION_EVENT_TYPES)
            transition_rows = conn.execute(
                f"""
                SELECT id, event_type, from_status, to_status
                FROM events
                WHERE event_type IN ({transition_placeholders})
                ORDER BY id ASC
                """,
                list(STATUS_TRANSITION_EVENT_TYPES),
            ).fetchall()
            for row in transition_rows:
                from_status = row["from_status"]
                to_status = row["to_status"]
                if from_status is None or to_status is None:
                    continue
                if from_status not in CLAIM_STATUSES or to_status not in CLAIM_STATUSES:
                    transition_issues.append(
                        {
                            "event_id": int(row["id"]),
                            "event_type": str(row["event_type"]),
                            "reason": "unknown_status",
                            "from_status": from_status,
                            "to_status": to_status,
                        }
                    )
                    continue
                if from_status == to_status:
                    continue
                from memorymaster.lifecycle import ALLOWED_TRANSITIONS

                if to_status not in ALLOWED_TRANSITIONS.get(str(from_status), set()):
                    transition_issues.append(
                        {
                            "event_id": int(row["id"]),
                            "event_type": str(row["event_type"]),
                            "reason": "invalid_transition",
                            "from_status": from_status,
                            "to_status": to_status,
                        }
                    )

            chain_issues: list[dict[str, object]] = []
            chain_rows = conn.execute(
                """
                SELECT id, prev_event_hash, event_hash, hash_algo
                FROM events
                ORDER BY id ASC
                """
            ).fetchall()
            expected_prev: str | None = None
            for row in chain_rows:
                row_prev = str(row["prev_event_hash"]) if row["prev_event_hash"] is not None else None
                row_hash = str(row["event_hash"]) if row["event_hash"] is not None else None
                row_algo = str(row["hash_algo"]) if row["hash_algo"] is not None else None
                if row_hash is None:
                    chain_issues.append({"event_id": int(row["id"]), "reason": "missing_hash"})
                    continue
                if row_algo not in {None, EVENT_HASH_ALGO}:
                    chain_issues.append(
                        {"event_id": int(row["id"]), "reason": "unexpected_hash_algo", "hash_algo": row_algo}
                    )
                if row_prev != expected_prev:
                    chain_issues.append(
                        {
                            "event_id": int(row["id"]),
                            "reason": "broken_prev_link",
                            "expected_prev_event_hash": expected_prev,
                            "actual_prev_event_hash": row_prev,
                        }
                    )
                expected_prev = row_hash

            issues = {
                "orphan_events": [int(row["id"]) for row in orphan_events],
                "orphan_citations": [int(row["id"]) for row in orphan_citations],
                "superseded_without_replacement": [int(row["id"]) for row in superseded_without_replacement],
                "dangling_replaced_by": [int(row["id"]) for row in dangling_replaced_by],
                "dangling_supersedes": [int(row["id"]) for row in dangling_supersedes],
                "transition_issues": transition_issues[:limit],
                "hash_chain_issues": chain_issues[:limit],
            }
            report["issues"] = issues
            report["summary"] = {
                key: (len(value) if isinstance(value, list) else 0)
                for key, value in issues.items()
            }

            actions: list[dict[str, object]] = []
            if fix:
                if issues["orphan_citations"]:
                    placeholders = ",".join("?" for _ in issues["orphan_citations"])
                    cur = conn.execute(f"DELETE FROM citations WHERE id IN ({placeholders})", issues["orphan_citations"])
                    actions.append({"action": "delete_orphan_citations", "rows": int(cur.rowcount)})
                if issues["orphan_events"]:
                    actions.append(
                        {
                            "action": "skip_delete_orphan_events_append_only",
                            "rows": 0,
                            "reason": "events table is append-only",
                        }
                    )
                if issues["dangling_replaced_by"]:
                    placeholders = ",".join("?" for _ in issues["dangling_replaced_by"])
                    cur = conn.execute(
                        f"UPDATE claims SET replaced_by_claim_id = NULL WHERE id IN ({placeholders})",
                        issues["dangling_replaced_by"],
                    )
                    actions.append({"action": "clear_dangling_replaced_by", "rows": int(cur.rowcount)})
                if issues["dangling_supersedes"]:
                    placeholders = ",".join("?" for _ in issues["dangling_supersedes"])
                    cur = conn.execute(
                        f"UPDATE claims SET supersedes_claim_id = NULL WHERE id IN ({placeholders})",
                        issues["dangling_supersedes"],
                    )
                    actions.append({"action": "clear_dangling_supersedes", "rows": int(cur.rowcount)})
                if issues["hash_chain_issues"]:
                    actions.append(
                        {
                            "action": "skip_rebuild_event_hash_chain_append_only",
                            "rows": 0,
                            "reason": "events table is append-only",
                        }
                    )
                conn.commit()
            report["actions"] = actions
        return report


    def record_event(
        self,
        *,
        claim_id: int | None,
        event_type: str,
        from_status: str | None = None,
        to_status: str | None = None,
        details: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        validated_event_type = validate_event_type(event_type)
        validated_payload = validate_event_payload(
            validated_event_type,
            payload,
            details=details,
        )
        now = utc_now()
        payload_json = json.dumps(validated_payload) if validated_payload is not None else None
        with self.connect() as conn:
            self._insert_event_row(
                conn,
                claim_id=claim_id,
                event_type=validated_event_type,
                from_status=from_status,
                to_status=to_status,
                details=details,
                payload_json=payload_json,
                created_at=now,
            )
            conn.commit()


    def upsert_embeddings(self, claims: list[Claim], provider: EmbeddingProvider) -> int:
        if not claims:
            return 0
        now = utc_now()
        rows = []
        for claim in claims:
            text = " ".join(
                part
                for part in [claim.text, claim.normalized_text or "", claim.subject or "", claim.object_value or ""]
                if part
            )
            embedding = provider.embed(text)
            rows.append((claim.id, provider.model, json.dumps(embedding), now))

        try:
            with self.connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO claim_embeddings (claim_id, model, embedding_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(claim_id) DO UPDATE SET
                        model = excluded.model,
                        embedding_json = excluded.embedding_json,
                        updated_at = excluded.updated_at
                    """,
                    rows,
                )
                conn.commit()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                logger.warning("claim_embeddings table missing, recreating: %s", exc)
                try:
                    # Create the table and ensure it's committed before retrying
                    with self.connect() as create_conn:
                        self._ensure_embeddings_schema(create_conn)
                        create_conn.commit()
                    # Retry the insert
                    with self.connect() as conn:
                        conn.executemany(
                            """
                            INSERT INTO claim_embeddings (claim_id, model, embedding_json, updated_at)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(claim_id) DO UPDATE SET
                                model = excluded.model,
                                embedding_json = excluded.embedding_json,
                                updated_at = excluded.updated_at
                            """,
                            rows,
                        )
                        conn.commit()
                except Exception as retry_exc:
                    logger.error("Failed to recreate claim_embeddings: %s", retry_exc)
                    return 0
            else:
                raise
        return len(rows)


    def vector_scores(
        self,
        query_text: str,
        claims: list[Claim],
        provider: EmbeddingProvider,
    ) -> dict[int, float]:
        if not claims:
            return {}
        self.upsert_embeddings(claims, provider)
        query_vec = provider.embed(query_text)
        claim_ids = [c.id for c in claims]
        placeholders = ",".join("?" for _ in claim_ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT claim_id, embedding_json FROM claim_embeddings WHERE claim_id IN ({placeholders})",
                claim_ids,
            ).fetchall()
        scores: dict[int, float] = {}
        for row in rows:
            emb = json.loads(str(row["embedding_json"]))
            sim = cosine_similarity(query_vec, emb)
            scores[int(row["claim_id"])] = max(0.0, min(1.0, (sim + 1.0) / 2.0))
        return scores


    def record_access(self, claim_id: int) -> None:
        """Increment access_count and set last_accessed for a claim."""
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE claims SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                (now, claim_id),
            )
            conn.commit()


    def record_accesses_batch(self, claim_ids: list[int]) -> None:
        """Batch update access_count and last_accessed for multiple claims in a single transaction.

        Much faster than calling record_access() in a loop when there are many claims.
        """
        if not claim_ids:
            return
        now = utc_now()
        with self.connect() as conn:
            # Use a single UPDATE statement with IN clause
            placeholders = ",".join("?" * len(claim_ids))
            conn.execute(
                f"UPDATE claims SET access_count = access_count + 1, last_accessed = ? WHERE id IN ({placeholders})",
                [now] + claim_ids,
            )
            conn.commit()


    def recompute_tiers(self) -> dict[str, int]:
        """Recompute tier for all non-archived claims based on access_count and age.

        Rules:
          - access_count > 5 OR created less than 7 days ago -> core
          - access_count = 0 AND created more than 90 days ago -> peripheral
          - everything else -> working
        """
        now = datetime.now(timezone.utc)
        core_cutoff = (now - timedelta(days=7)).replace(microsecond=0).isoformat()
        peripheral_cutoff = (now - timedelta(days=90)).replace(microsecond=0).isoformat()

        counts = {"core": 0, "working": 0, "peripheral": 0}
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE claims SET tier = 'core' "
                "WHERE status != 'archived' AND tier != 'core' "
                "AND (access_count > 5 OR created_at > ?)",
                (core_cutoff,),
            )
            counts["core"] = cur.rowcount

            cur = conn.execute(
                "UPDATE claims SET tier = 'peripheral' "
                "WHERE status != 'archived' AND tier != 'peripheral' "
                "AND access_count = 0 AND created_at <= ?",
                (peripheral_cutoff,),
            )
            counts["peripheral"] = cur.rowcount

            cur = conn.execute(
                "UPDATE claims SET tier = 'working' "
                "WHERE status != 'archived' AND tier != 'working' "
                "AND NOT (access_count > 5 OR created_at > ?) "
                "AND NOT (access_count = 0 AND created_at <= ?)",
                (core_cutoff, peripheral_cutoff),
            )
            counts["working"] = cur.rowcount

            conn.commit()
        return counts


    def add_claim_link(self, source_id: int, target_id: int, link_type: str) -> ClaimLink:
        if link_type not in CLAIM_LINK_TYPES:
            allowed = ", ".join(CLAIM_LINK_TYPES)
            raise ValueError(f"Invalid link_type '{link_type}'. Allowed: {allowed}.")
        if source_id == target_id:
            raise ValueError("source_id and target_id must be different.")
        now = utc_now()
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO claim_links (source_id, target_id, link_type, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (source_id, target_id, link_type, now),
                )
            except sqlite3.IntegrityError as exc:
                msg = str(exc).lower()
                if "unique" in msg:
                    raise ValueError(
                        f"Link already exists: {source_id} -> {target_id} ({link_type})."
                    ) from exc
                if "foreign key" in msg:
                    raise ValueError(
                        f"One or both claim ids do not exist: {source_id}, {target_id}."
                    ) from exc
                raise
            conn.commit()
            return ClaimLink(
                id=int(cur.lastrowid),
                source_id=source_id,
                target_id=target_id,
                link_type=link_type,
                created_at=now,
            )


    def remove_claim_link(self, source_id: int, target_id: int, link_type: str | None = None) -> int:
        with self.connect() as conn:
            if link_type is not None:
                cur = conn.execute(
                    "DELETE FROM claim_links WHERE source_id = ? AND target_id = ? AND link_type = ?",
                    (source_id, target_id, link_type),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM claim_links WHERE source_id = ? AND target_id = ?",
                    (source_id, target_id),
                )
            conn.commit()
            return cur.rowcount

