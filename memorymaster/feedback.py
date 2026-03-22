"""Usage feedback tracking — reward signal for claim quality.

Records which claims are returned in queries and which are accessed,
enabling quality scoring and future RL-based write policy.

Ported from MemoryKing's FeedbackTracker + RLWriteScorer, simplified
to work with memorymaster's existing access_count and tier system.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class FeedbackTracker:
    """Tracks claim usage patterns for quality scoring."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def ensure_tables(self) -> None:
        """Create feedback tables if they don't exist."""
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS usage_feedback (
                    id TEXT PRIMARY KEY,
                    claim_id INTEGER NOT NULL,
                    query_text TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    was_returned INTEGER NOT NULL DEFAULT 1,
                    score REAL
                );
                CREATE INDEX IF NOT EXISTS idx_uf_claim ON usage_feedback(claim_id);
                CREATE INDEX IF NOT EXISTS idx_uf_timestamp ON usage_feedback(timestamp);

                CREATE TABLE IF NOT EXISTS quality_scores (
                    claim_id INTEGER PRIMARY KEY,
                    quality_score REAL NOT NULL DEFAULT 0.5,
                    retrieval_count INTEGER NOT NULL DEFAULT 0,
                    last_scored TEXT NOT NULL,
                    factors TEXT
                );
            """)
            conn.commit()
        finally:
            conn.close()

    def record_retrieval(self, claim_ids: list[int], query_text: str) -> int:
        """Record that these claims were returned for a query.

        Returns 0 if claim_ids is empty or None.
        """
        if not claim_ids:
            return 0

        if not isinstance(claim_ids, list):
            logger.warning("record_retrieval: claim_ids is not a list, converting")
            try:
                claim_ids = list(claim_ids)
            except TypeError:
                return 0

        # Filter out None/invalid claim IDs
        claim_ids = [cid for cid in claim_ids if cid is not None and isinstance(cid, int) and cid > 0]
        if not claim_ids:
            return 0

        try:
            self.ensure_tables()
        except sqlite3.OperationalError as exc:
            logger.error("Failed to ensure feedback tables: %s", exc)
            return 0

        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (str(uuid.uuid4()), cid, query_text[:500] if query_text else "", now, 1, None)
            for cid in claim_ids
        ]
        conn = self._connect()
        try:
            conn.executemany(
                "INSERT INTO usage_feedback (id, claim_id, query_text, timestamp, was_returned, score) VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
            return len(rows)
        except sqlite3.OperationalError as exc:
            logger.error("Failed to record retrieval: %s", exc)
            conn.rollback()
            return 0
        finally:
            conn.close()

    def compute_quality_scores(self) -> dict[str, int]:
        """Recompute quality scores for all claims based on usage patterns.

        Quality score formula:
          base = 0.5
          + retrieval_bonus: min(retrieval_count * 0.05, 0.3)
          + access_bonus: min(access_count * 0.03, 0.2)  (from claims table)
          + freshness: 0.1 if accessed in last 7 days
          - staleness: -0.1 if never accessed and older than 30 days

        Returns dict with scored/updated counts. Returns {"scored": 0} for empty DB.
        """
        conn = self._connect()
        try:
            # Ensure tables exist
            try:
                conn.execute("SELECT 1 FROM quality_scores LIMIT 1")
            except sqlite3.OperationalError:
                try:
                    self.ensure_tables()
                except sqlite3.OperationalError as exc:
                    logger.error("Failed to ensure quality_scores table: %s", exc)
                    return {"scored": 0}

            now = datetime.now(timezone.utc).isoformat()

            # Get retrieval counts per claim
            retrieval_counts = {}
            try:
                for row in conn.execute(
                    "SELECT claim_id, COUNT(*) as cnt FROM usage_feedback GROUP BY claim_id"
                ).fetchall():
                    retrieval_counts[int(row["claim_id"])] = int(row["cnt"])
            except sqlite3.OperationalError:
                logger.debug("usage_feedback table doesn't exist yet")

            # Get all active claims with their access data
            try:
                claims = conn.execute(
                    "SELECT id, access_count, last_accessed, created_at, confidence FROM claims WHERE status != 'archived'"
                ).fetchall()
            except sqlite3.OperationalError:
                logger.warning("claims table missing or inaccessible")
                return {"scored": 0}

            if not claims:
                logger.debug("compute_quality_scores: no active claims found")
                return {"scored": 0}

            scored = 0
            for claim in claims:
                cid = int(claim["id"])
                access_count = int(claim["access_count"] or 0)
                retrieval_count = retrieval_counts.get(cid, 0)

                # Compute quality score
                base = 0.5
                retrieval_bonus = min(retrieval_count * 0.05, 0.3)
                access_bonus = min(access_count * 0.03, 0.2)

                # Freshness from last_accessed
                freshness = 0.0
                last_acc = claim["last_accessed"]
                if last_acc:
                    try:
                        days_since = (datetime.fromisoformat(now) - datetime.fromisoformat(last_acc)).days
                        freshness = 0.1 if days_since < 7 else 0.0
                    except (ValueError, TypeError):
                        pass

                # Staleness penalty
                staleness = 0.0
                if access_count == 0 and retrieval_count == 0:
                    try:
                        created = claim["created_at"]
                        if created:
                            age_days = (datetime.fromisoformat(now) - datetime.fromisoformat(created)).days
                            if age_days > 30:
                                staleness = -0.1
                    except (ValueError, TypeError):
                        pass

                quality = max(0.0, min(1.0, base + retrieval_bonus + access_bonus + freshness + staleness))

                factors = f"ret={retrieval_count},acc={access_count},fresh={freshness:.1f},stale={staleness:.1f}"

                conn.execute(
                    """INSERT INTO quality_scores (claim_id, quality_score, retrieval_count, last_scored, factors)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(claim_id) DO UPDATE SET
                           quality_score = excluded.quality_score,
                           retrieval_count = excluded.retrieval_count,
                           last_scored = excluded.last_scored,
                           factors = excluded.factors""",
                    (cid, quality, retrieval_count, now, factors),
                )
                scored += 1

            conn.commit()
            logger.info("Computed quality scores for %d claims", scored)
            return {"scored": scored}
        finally:
            conn.close()

    def get_top_quality(self, limit: int = 20) -> list[dict]:
        """Get claims ranked by quality score."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT qs.claim_id, qs.quality_score, qs.retrieval_count, qs.factors,
                          c.text, c.status, c.confidence, c.tier
                   FROM quality_scores qs
                   JOIN claims c ON c.id = qs.claim_id
                   ORDER BY qs.quality_score DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_stats(self) -> dict:
        """Return feedback tracking statistics."""
        conn = self._connect()
        try:
            feedback_count = conn.execute("SELECT COUNT(*) FROM usage_feedback").fetchone()[0]
            scored_count = conn.execute("SELECT COUNT(*) FROM quality_scores").fetchone()[0]
            avg_quality = conn.execute("SELECT AVG(quality_score) FROM quality_scores").fetchone()[0]
            return {
                "feedback_rows": feedback_count,
                "claims_scored": scored_count,
                "avg_quality": round(float(avg_quality or 0), 3),
            }
        finally:
            conn.close()
