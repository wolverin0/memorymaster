"""Backend-neutral capture job and lineage repository."""

from __future__ import annotations

import contextlib
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from memorymaster.capture.models import (
    CAPTURE_JOB_STATUSES,
    CAPTURE_STAGES,
    CaptureJob,
    ClaimEvidenceLink,
    EdgeSupport,
)
from memorymaster.core.models import EvidenceItem
from memorymaster.core.security import (
    sanitize_persisted_text,
    validate_persisted_metadata,
)

MAX_ATTEMPTS = 5
MAX_RETRY_SECONDS = 6 * 60 * 60


def graph_job_content_hash(claim_id: int, revision: Any) -> str:
    """Return the replay identity for one confirmed-claim graph revision."""
    value = f"claim:{int(claim_id)}:{revision}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _safe_detail(value: str | None) -> str | None:
    if value is None:
        return None
    sanitized, _ = sanitize_persisted_text(str(value))
    return sanitized[:1000]


def _mapping(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    raise TypeError("Capture repository requires mapping-compatible database rows.")


class CaptureRepository:
    """Persist lineage and lease jobs without putting schema work on read paths."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.postgres = hasattr(store, "dsn")
        self.placeholder = "%s" if self.postgres else "?"

    @contextlib.contextmanager
    def _connection(self) -> Iterator[Any]:
        conn = self.store.connect()
        try:
            yield conn
        except Exception:
            rollback = getattr(conn, "rollback", None)
            if callable(rollback):
                rollback()
            raise
        finally:
            conn.close()

    def _execute(self, conn: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
        if self.postgres:
            cur = conn.cursor()
            cur.execute(sql, params)
            return cur
        return conn.execute(sql, params)

    @staticmethod
    def _commit(conn: Any) -> None:
        conn.commit()

    def _job(self, row: Any) -> CaptureJob:
        data = _mapping(row)
        return CaptureJob(**{name: data.get(name) for name in CaptureJob.__dataclass_fields__})

    def queue_job(
        self,
        *,
        source_item_id: int,
        content_hash: str,
        stage: str,
        status: str = "pending",
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> tuple[CaptureJob, bool]:
        if source_item_id <= 0:
            raise ValueError("source_item_id must be positive")
        content_hash = content_hash.strip().lower()
        if len(content_hash) != 64 or any(ch not in "0123456789abcdef" for ch in content_hash):
            raise ValueError("content_hash must be a SHA-256 hex digest")
        if stage not in CAPTURE_STAGES:
            raise ValueError(f"unsupported capture stage: {stage}")
        if status not in CAPTURE_JOB_STATUSES - {"leased", "completed", "cancelled"}:
            raise ValueError(f"invalid initial capture status: {status}")
        validate_persisted_metadata({"stage": stage, "status": status, "error_code": error_code})
        stamp = _iso(_now())
        detail = _safe_detail(error_detail)
        with self._connection() as conn:
            sql = (
                """INSERT INTO capture_jobs
                   (source_item_id, content_hash, stage, status, attempts,
                    next_attempt_at, lease_owner, lease_expires_at,
                    error_code, error_detail, created_at, updated_at, completed_at)
                   VALUES (?, ?, ?, ?, 0, NULL, NULL, NULL, ?, ?, ?, ?, NULL)
                   ON CONFLICT(source_item_id, content_hash, stage) DO NOTHING"""
            )
            if self.postgres:
                sql = sql.replace("?", "%s")
            cur = self._execute(
                conn, sql, (source_item_id, content_hash, stage, status, error_code, detail, stamp, stamp)
            )
            created = bool(getattr(cur, "rowcount", 0))
            row = self._execute(
                conn,
                f"""SELECT * FROM capture_jobs
                    WHERE source_item_id = {self.placeholder}
                      AND content_hash = {self.placeholder}
                      AND stage = {self.placeholder}""",
                (source_item_id, content_hash, stage),
            ).fetchone()
            self._commit(conn)
        if row is None:
            raise RuntimeError("capture job insert returned no row")
        return self._job(row), created

    def _expire_leases(self, conn: Any, stamp: str) -> None:
        exhausted = (
            """UPDATE capture_jobs
               SET status='blocked', error_code='attempts_exhausted',
                   error_detail='Maximum capture attempts exhausted.',
                   lease_owner=NULL, lease_expires_at=NULL, updated_at=?
               WHERE status='leased' AND lease_expires_at <= ? AND attempts >= 5"""
        )
        retryable = (
            """UPDATE capture_jobs
               SET status='retryable', error_code='lease_expired',
                   error_detail='Worker lease expired before completion.',
                   next_attempt_at=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=?
               WHERE status='leased' AND lease_expires_at <= ? AND attempts < 5"""
        )
        if self.postgres:
            exhausted = exhausted.replace("?", "%s")
            retryable = retryable.replace("?", "%s")
        self._execute(conn, exhausted, (stamp, stamp))
        self._execute(conn, retryable, (stamp, stamp, stamp))

    def _select_due_job_ids(
        self,
        conn: Any,
        *,
        stamp: str,
        limit: int,
        stages: tuple[str, ...],
    ) -> list[int]:
        clauses = [
            "status IN ('pending','retryable')",
            "(next_attempt_at IS NULL OR next_attempt_at <= " + self.placeholder + ")",
            "attempts < 5",
        ]
        params: list[Any] = [stamp]
        if stages:
            marks = ",".join(self.placeholder for _ in stages)
            clauses.append(f"stage IN ({marks})")
            params.extend(stages)
        lock = " FOR UPDATE SKIP LOCKED" if self.postgres else ""
        rows = self._execute(
            conn,
            f"""SELECT id FROM capture_jobs WHERE {' AND '.join(clauses)}
                ORDER BY id LIMIT {self.placeholder}{lock}""",
            (*params, min(limit, 100)),
        ).fetchall()
        return [int(_mapping(row).get("id")) for row in rows]

    def _lease_selected(
        self,
        conn: Any,
        *,
        job_ids: list[int],
        owner: str,
        expiry: str,
        stamp: str,
    ) -> list[CaptureJob]:
        leased: list[CaptureJob] = []
        for job_id in job_ids:
            row = self._execute(
                conn,
                f"""UPDATE capture_jobs
                    SET status='leased', attempts=attempts+1,
                        lease_owner={self.placeholder}, lease_expires_at={self.placeholder},
                        error_code=NULL, error_detail=NULL, updated_at={self.placeholder}
                    WHERE id={self.placeholder}
                    RETURNING *""",
                (owner, expiry, stamp, job_id),
            ).fetchone()
            if row is not None:
                leased.append(self._job(row))
        return leased

    def lease_jobs(
        self,
        *,
        owner: str,
        limit: int = 25,
        lease_seconds: int = 300,
        stages: tuple[str, ...] | None = None,
    ) -> list[CaptureJob]:
        owner = owner.strip()
        validate_persisted_metadata({"lease_owner": owner})
        if not owner:
            raise ValueError("lease owner is required")
        if limit <= 0:
            return []
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        selected_stages = tuple(stages or ())
        if any(stage not in CAPTURE_STAGES for stage in selected_stages):
            raise ValueError("stages contains an unsupported value")
        now = _now()
        stamp = _iso(now)
        expiry = _iso(now + timedelta(seconds=lease_seconds))
        with self._connection() as conn:
            if not self.postgres:
                conn.execute("BEGIN IMMEDIATE")
            self._expire_leases(conn, stamp)
            ids = self._select_due_job_ids(
                conn, stamp=stamp, limit=limit, stages=selected_stages
            )
            leased = self._lease_selected(
                conn, job_ids=ids, owner=owner, expiry=expiry, stamp=stamp
            )
            self._commit(conn)
        return leased

    def finish_job(
        self,
        job_id: int,
        *,
        status: str,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> CaptureJob:
        if status not in {"completed", "cancelled", "blocked", "retryable"}:
            raise ValueError("finish status must be completed, cancelled, blocked, or retryable")
        validate_persisted_metadata({"status": status, "error_code": error_code})
        stamp = _iso(_now())
        detail = _safe_detail(error_detail)
        with self._connection() as conn:
            existing = self._execute(
                conn, f"SELECT attempts FROM capture_jobs WHERE id={self.placeholder}", (job_id,)
            ).fetchone()
            if existing is None:
                raise KeyError(f"capture job {job_id} not found")
            attempts = int(_mapping(existing)["attempts"])
            final_status = status
            next_attempt = None
            if status == "retryable":
                if attempts >= MAX_ATTEMPTS:
                    final_status = "blocked"
                    error_code = "attempts_exhausted"
                    detail = "Maximum capture attempts exhausted."
                else:
                    delay = min(MAX_RETRY_SECONDS, 60 * (2 ** max(0, attempts - 1)))
                    next_attempt = _iso(_now() + timedelta(seconds=delay))
            completed_at = stamp if final_status == "completed" else None
            row = self._execute(
                conn,
                f"""UPDATE capture_jobs
                    SET status={self.placeholder}, next_attempt_at={self.placeholder},
                        lease_owner=NULL, lease_expires_at=NULL,
                        error_code={self.placeholder}, error_detail={self.placeholder},
                        updated_at={self.placeholder}, completed_at={self.placeholder}
                    WHERE id={self.placeholder} RETURNING *""",
                (final_status, next_attempt, error_code, detail, stamp, completed_at, job_id),
            ).fetchone()
            self._commit(conn)
        if row is None:
            raise RuntimeError("capture job update returned no row")
        return self._job(row)

    def link_claim_evidence(
        self, *, claim_id: int, evidence_item_id: int, role: str = "support"
    ) -> tuple[ClaimEvidenceLink, bool]:
        role = role.strip().lower()
        validate_persisted_metadata({"role": role})
        if not role:
            raise ValueError("role is required")
        stamp = _iso(_now())
        sql = (
            """INSERT INTO claim_evidence_links
               (claim_id, evidence_item_id, role, created_at)
               VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING"""
        )
        if self.postgres:
            sql = sql.replace("?", "%s")
        with self._connection() as conn:
            cur = self._execute(conn, sql, (claim_id, evidence_item_id, role, stamp))
            created = bool(getattr(cur, "rowcount", 0))
            row = self._execute(
                conn,
                f"""SELECT * FROM claim_evidence_links
                    WHERE claim_id={self.placeholder}
                      AND evidence_item_id={self.placeholder}
                      AND role={self.placeholder}""",
                (claim_id, evidence_item_id, role),
            ).fetchone()
            self._commit(conn)
        return ClaimEvidenceLink(**_mapping(row)), created

    def add_edge_support(
        self,
        *,
        source_entity_id: int,
        target_entity_id: int,
        relation: str,
        supporting_claim_id: int,
        scope: str,
        ontology_version: str,
    ) -> tuple[EdgeSupport, bool]:
        relation = relation.strip().lower()
        validate_persisted_metadata(
            {"relation": relation, "scope": scope, "ontology_version": ontology_version}
        )
        stamp = _iso(_now())
        values = (
            source_entity_id,
            target_entity_id,
            relation,
            supporting_claim_id,
            scope,
            ontology_version,
            stamp,
        )
        sql = (
            """INSERT INTO entity_edge_supports
               (source_entity_id, target_entity_id, relation, supporting_claim_id,
                scope, ontology_version, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING"""
        )
        if self.postgres:
            sql = sql.replace("?", "%s")
        with self._connection() as conn:
            cur = self._execute(conn, sql, values)
            created = bool(getattr(cur, "rowcount", 0))
            row = self._execute(
                conn,
                f"""SELECT * FROM entity_edge_supports
                    WHERE source_entity_id={self.placeholder}
                      AND target_entity_id={self.placeholder}
                      AND relation={self.placeholder}
                      AND supporting_claim_id={self.placeholder}
                      AND scope={self.placeholder}
                      AND ontology_version={self.placeholder}""",
                values[:6],
            ).fetchone()
            self._commit(conn)
        return EdgeSupport(**_mapping(row)), created

    def retire_source(self, source_item_id: int, *, reason: str) -> bool:
        reason = (_safe_detail(reason) or "").strip()
        if not reason:
            raise ValueError("retirement reason is required")
        stamp = _iso(_now())
        observation_scopes: list[tuple[str, str | None]] = []
        with self._connection() as conn:
            if not self.postgres:
                observation_scopes = [
                    (str(row["scope"]), row["tenant_id"])
                    for row in conn.execute(
                        """SELECT DISTINCT go.scope, go.tenant_id
                           FROM graph_observation_supports gos
                           JOIN graph_observations go
                             ON go.observation_claim_id=gos.observation_claim_id
                           WHERE gos.source_item_id=?""",
                        (source_item_id,),
                    ).fetchall()
                ]
            cur = self._execute(
                conn,
                f"""UPDATE source_items SET retired_at={self.placeholder},
                    retirement_reason={self.placeholder}, updated_at={self.placeholder}
                    WHERE id={self.placeholder} AND retired_at IS NULL""",
                (stamp, reason, stamp, source_item_id),
            )
            changed = bool(getattr(cur, "rowcount", 0))
            self._execute(
                conn,
                f"""UPDATE capture_jobs SET status='cancelled', updated_at={self.placeholder}
                    WHERE source_item_id={self.placeholder}
                      AND status IN ('pending','leased','retryable')""",
                (stamp, source_item_id),
            )
            self._commit(conn)
        if changed and observation_scopes:
            from memorymaster.knowledge.graph_observation_engine import (
                invalidate_changed_observations,
            )
            from memorymaster.knowledge.graph_observation_repository import (
                GraphObservationRepository,
            )

            observations = GraphObservationRepository(self.store)
            for scope, tenant_id in observation_scopes:
                invalidate_changed_observations(
                    self.store,
                    observations,
                    scope=scope,
                    tenant_id=tenant_id,
                )
        return changed

    def status_counts(self) -> dict[str, int]:
        with self._connection() as conn:
            rows = self._execute(
                conn, "SELECT status, COUNT(*) AS count FROM capture_jobs GROUP BY status"
            ).fetchall()
        counts = {status: 0 for status in CAPTURE_JOB_STATUSES}
        for row in rows:
            data = _mapping(row)
            counts[str(data["status"])] = int(data["count"])
        return counts

    def evidence_for_content(
        self, *, source_item_id: int, content_hash: str
    ) -> EvidenceItem | None:
        with self._connection() as conn:
            row = self._execute(
                conn,
                f"""SELECT * FROM evidence_items
                    WHERE source_item_id={self.placeholder}
                      AND content_hash={self.placeholder}
                    ORDER BY id LIMIT 1""",
                (source_item_id, content_hash),
            ).fetchone()
        if row is None:
            return None
        converter = getattr(self.store, "_row_to_evidence_item")
        return converter(row)

    def claims_for_source(self, source_item_id: int) -> list[Any]:
        with self._connection() as conn:
            rows = self._execute(
                conn,
                f"""SELECT DISTINCT c.* FROM claims c
                    JOIN claim_evidence_links cel ON cel.claim_id=c.id
                    JOIN evidence_items e ON e.id=cel.evidence_item_id
                    WHERE e.source_item_id={self.placeholder}
                    ORDER BY c.id""",
                (source_item_id,),
            ).fetchall()
        converter = getattr(self.store, "_row_to_claim")
        return [converter(row) for row in rows]

    def active_supporting_source_count(
        self, claim_id: int, *, excluding_source_item_id: int | None = None
    ) -> int:
        clauses = ["cel.claim_id=" + self.placeholder, "s.retired_at IS NULL"]
        params: list[Any] = [claim_id]
        if excluding_source_item_id is not None:
            clauses.append("s.id<>" + self.placeholder)
            params.append(excluding_source_item_id)
        with self._connection() as conn:
            row = self._execute(
                conn,
                f"""SELECT COUNT(DISTINCT s.id) AS count
                    FROM claim_evidence_links cel
                    JOIN evidence_items e ON e.id=cel.evidence_item_id
                    JOIN source_items s ON s.id=e.source_item_id
                    WHERE {' AND '.join(clauses)}""",
                tuple(params),
            ).fetchone()
        return int(_mapping(row)["count"]) if row is not None else 0

    def due_evidence(self, *, scope: str, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        results: list[dict[str, Any]] = []
        cursor = 0
        page_size = min(max(limit * 4, 100), 800)
        with self._connection() as conn:
            while len(results) < limit:
                rows = self._execute(
                    conn,
                    f"""SELECT e.id AS evidence_id, e.source_item_id, e.content_hash,
                               s.content_hash AS source_hash, s.payload_json
                        FROM evidence_items e
                        JOIN source_items s ON s.id=e.source_item_id
                        WHERE e.id>{self.placeholder} AND s.retired_at IS NULL
                          AND e.text IS NOT NULL
                          AND NOT EXISTS (
                              SELECT 1 FROM capture_jobs j
                              WHERE j.source_item_id=s.id
                                AND j.content_hash=COALESCE(e.content_hash, s.content_hash)
                                AND j.stage='extract_claims')
                        ORDER BY e.id LIMIT {self.placeholder}""",
                    (cursor, page_size),
                ).fetchall()
                if not rows:
                    break
                cursor = int(_mapping(rows[-1])["evidence_id"])
                for row in rows:
                    data = _mapping(row)
                    payload = data.get("payload_json") or {}
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except json.JSONDecodeError:
                            payload = {}
                    if isinstance(payload, dict) and payload.get("scope") == scope:
                        results.append(data)
                    if len(results) >= limit:
                        break
        return results

    def due_confirmed_graph_claims(self, *, scope: str, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        results: list[dict[str, Any]] = []
        missing = 0
        cursor = 0
        page_size = min(max(limit, 50), 200)
        with self._connection() as conn:
            hashes = {
                str(_mapping(row)["content_hash"])
                for row in self._execute(
                    conn,
                    "SELECT content_hash FROM capture_jobs WHERE stage='extract_graph'",
                ).fetchall()
            }
            while missing < limit:
                rows = self._execute(
                    conn,
                    f"""SELECT c.id AS claim_id, c.updated_at,
                               COALESCE(
                                   (SELECT MAX(ev.created_at) FROM events ev
                                    WHERE ev.claim_id=c.id
                                      AND ev.to_status='confirmed'
                                      AND (ev.from_status IS NULL OR ev.from_status<>'confirmed')),
                                   c.updated_at
                               ) AS graph_revision,
                               MIN(e.source_item_id) AS source_item_id
                        FROM claims c
                        JOIN claim_evidence_links cel ON cel.claim_id=c.id
                        JOIN evidence_items e ON e.id=cel.evidence_item_id
                        JOIN source_items s ON s.id=e.source_item_id
                        WHERE c.id>{self.placeholder} AND c.status='confirmed'
                          AND COALESCE(c.claim_type, '') NOT IN
                              ('observation','skill','summary')
                          AND COALESCE(c.source_agent, '')<>
                              'memorymaster-graph-observer'
                          AND c.scope={self.placeholder} AND s.retired_at IS NULL
                        GROUP BY c.id, c.updated_at
                        ORDER BY c.id LIMIT {self.placeholder}""",
                    (cursor, scope, page_size),
                ).fetchall()
                if not rows:
                    break
                cursor = int(_mapping(rows[-1])["claim_id"])
                for row in rows:
                    data = _mapping(row)
                    digest = graph_job_content_hash(
                        data["claim_id"], data["graph_revision"]
                    )
                    exists = digest in hashes
                    results.append(
                        {**data, "job_content_hash": digest, "job_exists": exists}
                    )
                    missing += int(not exists)
                    if missing >= limit:
                        break
        return results

    def graph_job_identity(self, *, claim_id: int, updated_at: Any) -> str:
        """Use the latest transition into confirmed as the stable graph revision."""
        with self._connection() as conn:
            row = self._execute(
                conn,
                f"""SELECT MAX(created_at) AS confirmed_at FROM events
                    WHERE claim_id={self.placeholder} AND to_status='confirmed'
                      AND (from_status IS NULL OR from_status<>'confirmed')""",
                (claim_id,),
            ).fetchone()
        confirmed_at = _mapping(row).get("confirmed_at") if row is not None else None
        return graph_job_content_hash(claim_id, confirmed_at or updated_at)
