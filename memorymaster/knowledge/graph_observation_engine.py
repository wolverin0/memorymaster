"""Governed discovery, synthesis, and steward review for graph observations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.llm_provider import call_llm
from memorymaster.core.models import CitationInput
from memorymaster.knowledge.graph_observation_repository import (
    GraphObservationRepository,
    ObservationJob,
)
from memorymaster.knowledge.graph_observations import (
    ObservationComponent,
    ObservationDraft,
    discover_components,
    parse_synthesis_output,
)


MAX_SYNTHESIS_CALLS_PER_SCOPE = 3
OBSERVER_AGENT = "memorymaster-graph-observer"
SYSTEM_PROMPT = """You summarize one deterministic, evidence-bound graph component.
Return one JSON object only. Never add facts or claim IDs. The schema is either
{"decision":"no_signal"} or {"decision":"emit","name":"...",
"observation_type":"decision|commitment|constraint|dependency|state_change|recurring_pattern|stable_relationship|root_cause",
"summary":"...","assertions":[{"text":"...","supporting_claim_ids":[1]}]}.
Use no_signal when the supplied claims do not support a useful higher-level observation."""


@dataclass(frozen=True, slots=True)
class ObservationCycleResult:
    discovery_completed: int = 0
    synthesis_queued: int = 0
    synthesis_completed: int = 0
    emitted: int = 0
    no_signal: int = 0
    invalidated: int = 0
    failed: int = 0


def _component_from_job(
    repo: GraphObservationRepository, job: ObservationJob
) -> ObservationComponent | None:
    supports = repo.supports_from_manifest(
        scope=job.scope,
        tenant_id=job.tenant_id,
        manifest_json=job.support_manifest_json,
    )
    result = discover_components(supports, scope=job.scope, tenant_id=job.tenant_id)
    return next(
        (item for item in result.components if item.support_hash == job.support_hash),
        None,
    )


def _prompt(store: Any, component: ObservationComponent) -> str:
    claims = []
    for claim_id in component.claim_ids:
        claim = store.get_claim(claim_id, include_citations=False)
        if claim is None:
            raise ValueError("supporting claim disappeared")
        claims.append({"claim_id": claim.id, "text": claim.text})
    edges = [
        {
            "claim_id": row.claim_id,
            "evidence_id": row.evidence_id,
            "source_item_id": row.source_item_id,
            "signature": list(row.signature),
        }
        for row in component.supports
    ]
    return json.dumps(
        {"support_hash": component.support_hash, "claims": claims, "edges": edges},
        sort_keys=True,
    )


def _citations(component: ObservationComponent) -> list[CitationInput]:
    return [
        CitationInput(source="evidence", locator=f"evidence:{evidence_id}")
        for evidence_id in component.evidence_ids
    ]


def _create_candidate(
    store: Any,
    repo: GraphObservationRepository,
    draft: ObservationDraft,
    component: ObservationComponent,
) -> int:
    existing = repo.observation_for_support(
        scope=component.scope,
        tenant_id=component.tenant_id,
        support_hash=component.support_hash,
    )
    if existing is not None:
        return existing
    claim = store.create_claim(
        text=draft.summary,
        citations=_citations(component),
        idempotency_key=f"graph-observation:{component.support_hash}",
        claim_type="observation",
        subject=draft.name,
        predicate="graph_observation",
        object_value=draft.observation_type,
        scope=component.scope,
        confidence=min(row.confidence for row in component.supports),
        tenant_id=component.tenant_id,
        source_agent=OBSERVER_AGENT,
    )
    repo.persist_observation(
        observation_claim_id=claim.id, draft=draft, component=component
    )
    return claim.id


def invalidate_changed_observations(
    store: Any,
    repo: GraphObservationRepository,
    *,
    scope: str,
    tenant_id: str | None,
    current_hashes: set[str] | None = None,
) -> int:
    if current_hashes is None:
        active = repo.load_active_supports(scope=scope, tenant_id=tenant_id)
        current_hashes = {
            item.support_hash
            for item in discover_components(
                active, scope=scope, tenant_id=tenant_id
            ).components
        }
    changed = 0
    for row in repo.scope_observations(scope=scope, tenant_id=tenant_id):
        status = str(row["status"])
        if status not in {"candidate", "confirmed"}:
            continue
        reason = "support_fingerprint_changed"
        if row["support_hash"] in current_hashes:
            eligible, reason = repo.observation_gate(int(row["observation_claim_id"]))
            if eligible:
                continue
        target = "archived" if status == "candidate" else "stale"
        transition_claim(
            store,
            int(row["observation_claim_id"]),
            target,
            f"graph-observation support gate failed: {reason}",
            event_type="staleness",
        )
        changed += 1
    return changed


class GraphObservationEngine:
    def __init__(
        self,
        store: Any,
        *,
        llm_call: Callable[[str, str], str] = call_llm,
    ) -> None:
        self.store = store
        self.repo = GraphObservationRepository(store)
        self.llm_call = llm_call

    def process_discovery(self, *, owner: str, scope: str, limit: int = 10) -> ObservationCycleResult:
        completed = queued = invalidated = failed = 0
        jobs = self.repo.lease_jobs(
            owner=owner, limit=limit, stages=("discover",), scope=scope
        )
        for job in jobs:
            try:
                supports = self.repo.load_active_supports(
                    scope=job.scope, tenant_id=job.tenant_id
                )
                result = discover_components(
                    supports, scope=job.scope, tenant_id=job.tenant_id
                )
                hashes = {component.support_hash for component in result.components}
                invalidated += invalidate_changed_observations(
                    self.store,
                    self.repo,
                    scope=job.scope,
                    tenant_id=job.tenant_id,
                    current_hashes=hashes,
                )
                for component in result.components:
                    if self._queue_component(job, component):
                        queued += 1
                codes = [item.code for item in result.diagnostics]
                self.repo.complete_job(job.id, owner=owner, diagnostic_codes=codes)
                completed += 1
            except Exception:  # noqa: BLE001 - typed retry boundary persisted below
                self.repo.fail_job(job.id, owner=owner, error_code="discovery_failed")
                failed += 1
        return ObservationCycleResult(completed, queued, invalidated=invalidated, failed=failed)

    def _queue_component(self, job: ObservationJob, component: ObservationComponent) -> bool:
        if self.repo.observation_for_support(
            scope=component.scope,
            tenant_id=component.tenant_id,
            support_hash=component.support_hash,
        ) is not None:
            return False
        _queued, created = self.repo.queue_job(
            tenant_id=component.tenant_id,
            scope=component.scope,
            stage="synthesize",
            content_hash=component.support_hash,
            support_hash=component.support_hash,
            ontology_version=job.ontology_version,
            support_manifest=self.repo.component_manifest(component),
        )
        return created

    def process_synthesis(self, *, owner: str, scope: str) -> ObservationCycleResult:
        completed = emitted = no_signal = failed = 0
        jobs = self.repo.lease_jobs(
            owner=owner,
            limit=MAX_SYNTHESIS_CALLS_PER_SCOPE,
            stages=("synthesize",),
            scope=scope,
        )
        for job in jobs:
            try:
                component = _component_from_job(self.repo, job)
                if component is None:
                    self.repo.fail_job(job.id, owner=owner, error_code="support_changed")
                    failed += 1
                    continue
                raw = self.llm_call(SYSTEM_PROMPT, _prompt(self.store, component))
                draft = parse_synthesis_output(raw, allowed_claim_ids=component.claim_ids)
                if draft.decision == "emit":
                    _create_candidate(self.store, self.repo, draft, component)
                    emitted += 1
                else:
                    no_signal += 1
                self.repo.complete_job(job.id, owner=owner)
                completed += 1
            except Exception:  # noqa: BLE001 - fail closed and retry from IDs
                self.repo.fail_job(job.id, owner=owner, error_code="synthesis_failed")
                failed += 1
        return ObservationCycleResult(
            synthesis_completed=completed,
            emitted=emitted,
            no_signal=no_signal,
            failed=failed,
        )


def review_observation_candidates(
    store: Any,
    *,
    scope: str | None = None,
    limit: int = 50,
    apply: bool = True,
) -> dict[str, int]:
    repo = GraphObservationRepository(store)
    with repo._connection() as conn:
        params: tuple[Any, ...] = () if scope is None else (scope,)
        clause = "" if scope is None else "AND scope=?"
        rows = conn.execute(
            f"""SELECT id FROM claims WHERE status='candidate'
                AND claim_type='observation' {clause} ORDER BY id LIMIT ?""",
            (*params, limit),
        ).fetchall()
    stats = {"checked": len(rows), "confirmed": 0, "archived": 0, "would_confirm": 0}
    for row in rows:
        claim_id = int(row["id"])
        eligible, reason = repo.observation_gate(claim_id)
        if eligible:
            if apply:
                transition_claim(
                    store,
                    claim_id,
                    "confirmed",
                    "graph-observation deterministic support gate passed",
                    event_type="deterministic_validator",
                )
                stats["confirmed"] += 1
            else:
                stats["would_confirm"] += 1
        elif apply:
            transition_claim(
                store,
                claim_id,
                "archived",
                f"graph-observation gate failed: {reason}",
                event_type="deterministic_validator",
            )
            stats["archived"] += 1
    return stats
