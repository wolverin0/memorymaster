"""SQLite repository for replay-safe graph-observation work and lineage."""

from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator

from memorymaster.knowledge.graph_observations import (
    ALGORITHM_VERSION,
    ObservationComponent,
    ObservationDraft,
    ObservationSupport,
    canonical_signature,
    discover_components,
    support_fingerprint,
)
from memorymaster.knowledge.ontology import load_ontology


MAX_ATTEMPTS = 5
MAX_RETRY_SECONDS = 6 * 60 * 60
JOB_STATUSES = frozenset(
    {"pending", "leased", "retryable", "blocked", "completed", "cancelled"}
)
JOB_STAGES = frozenset({"discover", "synthesize"})
# A completed job MUST declare which of these it reached. ``status='completed'``
# alone answers "did the machine run", never "did any work happen".
JOB_OUTCOMES = frozenset(
    {
        "components_found",
        "no_supports",
        "no_components",
        "observation_emitted",
        "no_signal",
    }
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _mapping(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    raise TypeError("graph observation repository requires mapping-compatible rows")


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _observation_support(row: Any, symmetric: frozenset[str]) -> ObservationSupport:
    data = _mapping(row)
    signature = canonical_signature(
        data["source_entity_id"],
        str(data["relation"]).strip().lower(),
        data["target_entity_id"],
        data["ontology_version"],
        symmetric_relations=symmetric,
    )
    return ObservationSupport(
        claim_id=int(data["claim_id"]),
        evidence_id=int(data["evidence_id"]),
        source_item_id=int(data["source_item_id"]),
        source_entity_id=signature[0],
        relation=signature[1],
        target_entity_id=signature[2],
        ontology_version=signature[3],
        scope=str(data["scope"]),
        tenant_id=data["tenant_id"],
        confidence=float(data["confidence"]),
        occurred_at=data["occurred_at"],
    )


@dataclass(frozen=True, slots=True)
class ObservationJob:
    id: int
    tenant_id: str | None
    scope: str
    stage: str
    status: str
    content_hash: str
    support_hash: str | None
    algorithm_version: str
    ontology_version: str
    support_manifest_json: str
    attempts: int
    next_attempt_at: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    error_code: str | None
    diagnostic_hash: str | None
    outcome: str | None
    diagnostic_codes: str | None
    created_at: str
    updated_at: str
    completed_at: str | None


class GraphObservationRepository:
    """Persist PPR-7 state while explicitly rejecting PostgreSQL runtimes."""

    def __init__(self, store: Any) -> None:
        if hasattr(store, "dsn"):
            raise RuntimeError("graph observations are SQLite-only")
        self.store = store

    @contextlib.contextmanager
    def _connection(self) -> Iterator[Any]:
        conn = self.store.connect()
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _job(row: Any) -> ObservationJob:
        data = _mapping(row)
        fields = ObservationJob.__dataclass_fields__
        return ObservationJob(**{name: data.get(name) for name in fields})

    def queue_job(self, *, tenant_id: str | None, scope: str, stage: str, content_hash: str, ontology_version: str, support_hash: str | None = None, support_manifest: Iterable[Iterable[Any]] = ()) -> tuple[ObservationJob, bool]:
        if stage not in JOB_STAGES or len(content_hash) != 64:
            raise ValueError("invalid graph observation job identity")
        manifest_json = json.dumps(
            sorted(list(item) for item in support_manifest), separators=(",", ":")
        )
        stamp = _iso(_now())
        values = (
            tenant_id,
            scope,
            stage,
            "pending",
            content_hash,
            support_hash,
            ALGORITHM_VERSION,
            ontology_version,
            manifest_json,
            stamp,
            stamp,
        )
        with self._connection() as conn:
            cur = conn.execute(
                """INSERT INTO graph_observation_jobs
                   (tenant_id, scope, stage, status, content_hash, support_hash,
                    algorithm_version, ontology_version, support_manifest_json,
                    attempts, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                   ON CONFLICT DO NOTHING""",
                values,
            )
            created = cur.rowcount > 0
            row = conn.execute(
                """SELECT * FROM graph_observation_jobs
                   WHERE tenant_id IS ? AND scope=? AND stage=? AND content_hash=?
                     AND algorithm_version=? AND ontology_version=?""",
                (tenant_id, scope, stage, content_hash, ALGORITHM_VERSION, ontology_version),
            ).fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError("graph observation job insert returned no row")
        return self._job(row), created

    def queue_discovery(
        self,
        *,
        tenant_id: str | None,
        scope: str,
        ontology_version: str,
        cycle_hour: str,
    ) -> tuple[ObservationJob, bool]:
        identity = _digest(
            ["discover", tenant_id or "", scope, cycle_hour, ALGORITHM_VERSION, ontology_version]
        )
        return self.queue_job(
            tenant_id=tenant_id,
            scope=scope,
            stage="discover",
            content_hash=identity,
            ontology_version=ontology_version,
        )

    def _expire_leases(self, conn: Any, stamp: str) -> None:
        conn.execute(
            """UPDATE graph_observation_jobs
               SET status='blocked', error_code='attempts_exhausted',
                   lease_owner=NULL, lease_expires_at=NULL, updated_at=?
               WHERE status='leased' AND lease_expires_at<=? AND attempts>=5""",
            (stamp, stamp),
        )
        conn.execute(
            """UPDATE graph_observation_jobs
               SET status='retryable', error_code='lease_expired', next_attempt_at=?,
                   lease_owner=NULL, lease_expires_at=NULL, updated_at=?
               WHERE status='leased' AND lease_expires_at<=? AND attempts<5""",
            (stamp, stamp, stamp),
        )

    def lease_jobs(
        self,
        *,
        owner: str,
        limit: int,
        lease_seconds: int = 300,
        stages: tuple[str, ...] = (),
        scope: str | None = None,
    ) -> list[ObservationJob]:
        if not owner.strip() or limit <= 0:
            return []
        if any(stage not in JOB_STAGES for stage in stages):
            raise ValueError("invalid graph observation stage filter")
        stamp = _iso(_now())
        expiry = _iso(_now() + timedelta(seconds=max(1, lease_seconds)))
        with self._connection() as conn:
            self._expire_leases(conn, stamp)
            clauses = ["status IN ('pending','retryable')", "attempts<5"]
            clauses.append("(next_attempt_at IS NULL OR next_attempt_at<=?)")
            params: list[Any] = [stamp]
            if stages:
                clauses.append(f"stage IN ({','.join('?' for _ in stages)})")
                params.extend(stages)
            if scope is not None:
                clauses.append("scope=?")
                params.append(scope)
            ids = [
                int(row["id"])
                for row in conn.execute(
                    f"""SELECT id FROM graph_observation_jobs
                        WHERE {' AND '.join(clauses)} ORDER BY id LIMIT ?""",
                    (*params, limit),
                ).fetchall()
            ]
            if ids:
                marks = ",".join("?" for _ in ids)
                conn.execute(
                    f"""UPDATE graph_observation_jobs
                        SET status='leased', attempts=attempts+1, lease_owner=?,
                            lease_expires_at=?, updated_at=? WHERE id IN ({marks})""",
                    (owner, expiry, stamp, *ids),
                )
            rows = conn.execute(
                f"SELECT * FROM graph_observation_jobs WHERE id IN ({','.join('?' for _ in ids)})"
                if ids
                else "SELECT * FROM graph_observation_jobs WHERE 0",
                tuple(ids),
            ).fetchall()
            conn.commit()
        return [self._job(row) for row in sorted(rows, key=lambda row: int(row["id"]))]

    def complete_job(
        self,
        job_id: int,
        *,
        owner: str,
        outcome: str,
        diagnostic_codes: Iterable[str] = (),
    ) -> bool:
        """Complete a job with the outcome it actually reached.

        ``outcome`` is required: a caller that cannot say what the job concluded
        has no business marking it completed. The diagnostic codes are stored
        verbatim — hashing them destroyed the "why" at write time.
        """
        if outcome not in JOB_OUTCOMES:
            raise ValueError(f"unknown graph observation job outcome: {outcome!r}")
        stamp = _iso(_now())
        codes = sorted(set(str(code) for code in diagnostic_codes))
        with self._connection() as conn:
            cur = conn.execute(
                """UPDATE graph_observation_jobs
                   SET status='completed', completed_at=?, updated_at=?,
                       lease_owner=NULL, lease_expires_at=NULL,
                       outcome=?, diagnostic_codes=?
                   WHERE id=? AND status='leased' AND lease_owner=?""",
                (
                    stamp,
                    stamp,
                    outcome,
                    json.dumps(codes, separators=(",", ":")) if codes else None,
                    job_id,
                    owner,
                ),
            )
            conn.commit()
        return cur.rowcount > 0

    def fail_job(
        self, job_id: int, *, owner: str, error_code: str, detail: str | None = None
    ) -> bool:
        """Marca el job fallido y GUARDA la causa, no solo la etiqueta.

        `error_code` clasifica ("synthesis_failed"); `detail` dice por que. Sin
        el segundo, cinco intentos dejaban cinco veces la misma palabra y cero
        informacion: la razon real —un proveedor dado de baja— se destruia al
        escribir. Se apoya en `diagnostic_codes`, la columna de texto legible que
        la migracion 0022 agrego exactamente para esto.

        Se trunca a 500 chars: un traceback entero no aporta sobre la primera
        linea y esta tabla no es un log.
        """
        stamp_dt = _now()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT attempts FROM graph_observation_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                return False
            attempts = int(row["attempts"])
            blocked = attempts >= MAX_ATTEMPTS
            delay = min(MAX_RETRY_SECONDS, 60 * (2 ** max(0, attempts - 1)))
            next_attempt = None if blocked else _iso(stamp_dt + timedelta(seconds=delay))
            cur = conn.execute(
                """UPDATE graph_observation_jobs
                   SET status=?, error_code=?, diagnostic_codes=?, next_attempt_at=?,
                       updated_at=?, lease_owner=NULL, lease_expires_at=NULL
                   WHERE id=? AND status='leased' AND lease_owner=?""",
                (
                    "blocked" if blocked else "retryable",
                    error_code,
                    (detail or "")[:500] or None,
                    next_attempt,
                    _iso(stamp_dt),
                    job_id,
                    owner,
                ),
            )
            conn.commit()
        return cur.rowcount > 0

    def load_active_supports(
        self, *, scope: str, tenant_id: str | None
    ) -> tuple[ObservationSupport, ...]:
        ontology = load_ontology()
        symmetric = frozenset(
            name for name, definition in ontology.relations.items() if definition.symmetric
        )
        relations = tuple(sorted(ontology.relations))
        relation_marks = ",".join("?" for _ in relations)
        with self._connection() as conn:
            rows = conn.execute(
                f"""SELECT ees.supporting_claim_id AS claim_id,
                          cel.evidence_item_id AS evidence_id,
                          e.source_item_id, ees.source_entity_id, ees.relation,
                          ees.target_entity_id, ees.ontology_version, c.scope,
                          c.tenant_id, c.confidence, s.occurred_at
                   FROM entity_edge_supports ees
                   JOIN claims c ON c.id=ees.supporting_claim_id
                   JOIN claim_evidence_links cel ON cel.claim_id=c.id
                   JOIN evidence_items e ON e.id=cel.evidence_item_id
                   JOIN source_items s ON s.id=e.source_item_id
                   WHERE c.scope=? AND c.tenant_id IS ? AND ees.scope=c.scope
                     AND c.status='confirmed' AND c.visibility<>'sensitive'
                     AND COALESCE(c.claim_type, '') NOT IN ('observation','skill','summary')
                     AND COALESCE(c.source_agent, '')<>'memorymaster-graph-observer'
                     AND s.retired_at IS NULL
                     AND s.sensitivity='none' AND e.sensitivity='none'
                     AND ees.ontology_version=?
                     AND ees.relation IN ({relation_marks})
                   ORDER BY cel.evidence_item_id, c.id, ees.source_entity_id,
                            ees.relation, ees.target_entity_id, ees.ontology_version""",
                (scope, tenant_id, ontology.version, *relations),
            ).fetchall()
        return tuple(_observation_support(row, symmetric) for row in rows)

    @staticmethod
    def component_manifest(component: ObservationComponent) -> list[list[Any]]:
        return [
            [
                row.claim_id,
                row.evidence_id,
                row.source_item_id,
                row.source_entity_id,
                row.relation,
                row.target_entity_id,
                row.ontology_version,
            ]
            for row in component.supports
        ]

    def supports_from_manifest(
        self,
        *,
        scope: str,
        tenant_id: str | None,
        manifest_json: str,
    ) -> tuple[ObservationSupport, ...]:
        requested = {tuple(row) for row in json.loads(manifest_json)}
        active = self.load_active_supports(scope=scope, tenant_id=tenant_id)
        return tuple(
            row
            for row in active
            if (
                row.claim_id,
                row.evidence_id,
                row.source_item_id,
                row.source_entity_id,
                row.relation,
                row.target_entity_id,
                row.ontology_version,
            )
            in requested
        )

    def support_is_current(
        self,
        *,
        scope: str,
        tenant_id: str | None,
        manifest_json: str,
        expected_hash: str,
    ) -> tuple[bool, tuple[ObservationSupport, ...]]:
        requested = json.loads(manifest_json)
        active = self.supports_from_manifest(
            scope=scope, tenant_id=tenant_id, manifest_json=manifest_json
        )
        current = len(active) == len(requested) and support_fingerprint(active) == expected_hash
        claims = {row.claim_id for row in active}
        evidence = {row.evidence_id for row in active}
        sources = {row.source_item_id for row in active}
        confidence = min((row.confidence for row in active), default=0.0)
        gate = len(claims) >= 3 and len(evidence) >= 2 and len(sources) >= 2
        return current and gate and confidence >= 0.65, active

    def persist_supports(
        self, observation_claim_id: int, component: ObservationComponent
    ) -> None:
        stamp = _iso(_now())
        with self._connection() as conn:
            for row in component.supports:
                conn.execute(
                    """INSERT OR IGNORE INTO graph_observation_supports
                       (observation_claim_id, supporting_claim_id, evidence_item_id,
                        source_item_id, source_entity_id, target_entity_id, relation,
                        ontology_version, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        observation_claim_id,
                        row.claim_id,
                        row.evidence_id,
                        row.source_item_id,
                        row.source_entity_id,
                        row.target_entity_id,
                        row.relation,
                        row.ontology_version,
                        stamp,
                    ),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO claim_links
                       (source_id, target_id, link_type, created_at)
                       VALUES (?, ?, 'derived_from', ?)""",
                    (observation_claim_id, row.claim_id, stamp),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO claim_evidence_links
                       (claim_id, evidence_item_id, role, created_at)
                       VALUES (?, ?, 'observation_support', ?)""",
                    (observation_claim_id, row.evidence_id, stamp),
                )
            conn.commit()

    def persist_observation(
        self,
        *,
        observation_claim_id: int,
        draft: ObservationDraft,
        component: ObservationComponent,
    ) -> None:
        stamp = _iso(_now())
        ontology_versions = {row.ontology_version for row in component.supports}
        if len(ontology_versions) != 1:
            raise ValueError("one observation cannot span ontology versions")
        with self._connection() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO graph_observations
                   (observation_claim_id, observation_type, name, scope, tenant_id,
                    support_hash, algorithm_version, ontology_version,
                    evidence_window_start, evidence_window_end, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation_claim_id,
                    draft.observation_type,
                    draft.name,
                    component.scope,
                    component.tenant_id,
                    component.support_hash,
                    ALGORITHM_VERSION,
                    next(iter(ontology_versions)),
                    component.evidence_window_start,
                    component.evidence_window_end,
                    stamp,
                    stamp,
                ),
            )
            conn.commit()
        self.persist_supports(observation_claim_id, component)

    def observation_gate(self, observation_claim_id: int) -> tuple[bool, str]:
        with self._connection() as conn:
            row = conn.execute(
                """SELECT scope, tenant_id, support_hash FROM graph_observations
                   WHERE observation_claim_id=?""",
                (observation_claim_id,),
            ).fetchone()
        if row is None:
            return False, "observation_metadata_missing"
        active = self.load_active_supports(scope=row["scope"], tenant_id=row["tenant_id"])
        discovery = discover_components(
            active, scope=str(row["scope"]), tenant_id=row["tenant_id"]
        )
        component = next(
            (item for item in discovery.components if item.support_hash == row["support_hash"]),
            None,
        )
        if component is None:
            return False, "support_fingerprint_changed"
        weakest = min(item.confidence for item in component.supports)
        if len(component.source_item_ids) < 2:
            return False, "independent_sources_below_minimum"
        if weakest < 0.65:
            return False, "support_confidence_below_minimum"
        return True, "eligible"

    def scope_observations(
        self, *, scope: str, tenant_id: str | None
    ) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT go.*, c.status, c.text, c.confidence, c.created_at AS claim_created_at
                   FROM graph_observations go
                   JOIN claims c ON c.id=go.observation_claim_id
                   WHERE go.scope=? AND go.tenant_id IS ?
                   ORDER BY go.observation_claim_id""",
                (scope, tenant_id),
            ).fetchall()
        return [_mapping(row) for row in rows]

    def observation_support_rows(self, observation_claim_id: int) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT supporting_claim_id, evidence_item_id, source_item_id,
                          source_entity_id, relation, target_entity_id, ontology_version
                   FROM graph_observation_supports
                   WHERE observation_claim_id=?
                   ORDER BY supporting_claim_id, evidence_item_id,
                            source_entity_id, relation, target_entity_id""",
                (observation_claim_id,),
            ).fetchall()
        return [_mapping(row) for row in rows]

    def observation_for_support(
        self, *, scope: str, tenant_id: str | None, support_hash: str
    ) -> int | None:
        with self._connection() as conn:
            row = conn.execute(
                """SELECT observation_claim_id FROM graph_observations
                   WHERE scope=? AND tenant_id IS ? AND support_hash=?
                     AND algorithm_version=?""",
                (scope, tenant_id, support_hash, ALGORITHM_VERSION),
            ).fetchone()
        return int(row["observation_claim_id"]) if row else None

    def status_counts(
        self, *, tenant_id: str | None = None, scope: str | None = None
    ) -> dict[str, dict[str, int]]:
        counts = {stage: {status: 0 for status in JOB_STATUSES} for stage in JOB_STAGES}
        params: list[Any] = [tenant_id]
        scope_sql = ""
        if scope:
            scope_sql = " AND scope=?"
            params.append(scope)
        with self._connection() as conn:
            rows = conn.execute(
                f"""SELECT stage, status, COUNT(*) AS count
                    FROM graph_observation_jobs
                    WHERE tenant_id IS ?{scope_sql}
                    GROUP BY stage, status""",
                tuple(params),
            ).fetchall()
        for row in rows:
            counts[str(row["stage"])][str(row["status"])] = int(row["count"])
        return counts

    def outcome_counts(
        self, *, tenant_id: str | None = None, scope: str | None = None
    ) -> dict[str, dict[str, int]]:
        """Break completed jobs down by what they concluded.

        ``status_counts`` alone reports 3,146 completed discovery jobs against 2
        observations and reads as success. This is the companion that says how
        many of those completions found anything.
        """
        counts = {
            stage: {outcome: 0 for outcome in sorted(JOB_OUTCOMES)} | {"unrecorded": 0}
            for stage in JOB_STAGES
        }
        params: list[Any] = [tenant_id]
        scope_sql = ""
        if scope:
            scope_sql = " AND scope=?"
            params.append(scope)
        with self._connection() as conn:
            rows = conn.execute(
                f"""SELECT stage, outcome, COUNT(*) AS count
                    FROM graph_observation_jobs
                    WHERE tenant_id IS ?{scope_sql} AND status='completed'
                    GROUP BY stage, outcome""",
                tuple(params),
            ).fetchall()
        for row in rows:
            outcome = str(row["outcome"]) if row["outcome"] else "unrecorded"
            counts[str(row["stage"])][outcome] = int(row["count"])
        return counts
