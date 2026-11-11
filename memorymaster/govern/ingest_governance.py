"""Govern mutable-state admission and explicit supersession intent.

Keeps the service facade small while preserving candidate-only writes,
identity-bound replacement proposals, and auditable volatility overrides.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from memorymaster.core.event_scan import PROPOSAL_SCAN_CEILING, window_start
from memorymaster.core.intake_policy import IntakeRejected
from memorymaster.core.models import Claim
from memorymaster.core.temporal_policy import (
    MutableStateDecision,
    classify_mutable_state,
)
from memorymaster.govern.correction_text import Reference, find_corrected_references

logger = logging.getLogger(__name__)

MAX_SUPERSEDES = 5  # per call; no caller needs more (checkpoint items carry their own list)
# Declared (`supersedes`) proposals one agent may file per rolling day. `source_agent` is self-declared, so this
# bounds what a junk or compromised caller can queue; further ones are refused, nothing written.
MAX_DECLARED_PER_AGENT_PER_DAY = 25
INACTIVE_STATUSES = frozenset({"archived", "superseded"})
# Proposal ``source`` values. Text-inferred proposals are operator-only (govern.steward.OPERATOR_ONLY_SOURCES):
# the curation drain must not apply a guess made from prose.
SOURCE_DIRECT_INGEST = "direct-ingest"
PROPOSAL_DETAILS = "steward_proposal:superseded_candidate"
SOURCE_PROSE_CORRECTION = "prose-correction"


@dataclass(frozen=True, slots=True)
class SupersessionTarget:
    claim: Claim
    cross_identity: bool = False


@dataclass(frozen=True, slots=True)
class IngestGovernanceDecision:
    mutable_state: MutableStateDecision
    supersession_targets: tuple[SupersessionTarget, ...]
    requested_volatility: str


def prepare_ingest_governance(
    store: Any,
    *,
    text: str,
    claim_type: str | None,
    supersedes_claim_id: int | None,
    tenant_id: str | None,
    scope: str,
    volatility: str,
    valid_until: str | None,
    visibility: str,
    source_agent: str | None,
    supersedes: Sequence[str | int] | str | int | None = None,
) -> IngestGovernanceDecision:
    mutable_state = govern_mutable_state(
        text=text,
        claim_type=claim_type,
        scope=scope,
        volatility=volatility,
        valid_until=valid_until,
    )
    targets: list[SupersessionTarget] = []
    legacy, cross_identity = resolve_supersession_target(
        store,
        supersedes_claim_id,
        tenant_id=tenant_id,
        scope=scope,
        visibility=visibility,
        source_agent=source_agent,
    )
    if legacy is not None:
        targets.append(SupersessionTarget(legacy, cross_identity))
    declared = resolve_explicit_supersedes(
        store, supersedes, tenant_id=tenant_id, scope=scope, visibility=visibility, source_agent=source_agent
    )
    if declared:
        enforce_daily_bound(store, source_agent, len(declared))
    for claim in declared:
        if all(claim.id != known.claim.id for known in targets):
            # another agent's claim: operator-only proposal, never applied by the curation drain
            targets.append(SupersessionTarget(claim, (claim.visibility, claim.source_agent) != (visibility, source_agent)))
    return IngestGovernanceDecision(mutable_state, tuple(targets), volatility)


def govern_mutable_state(
    *,
    text: str,
    claim_type: str | None,
    scope: str,
    volatility: str,
    valid_until: str | None,
) -> MutableStateDecision:
    decision = classify_mutable_state(
        text=text,
        claim_type=claim_type,
        volatility=volatility,
        valid_until=valid_until,
    )
    if decision.is_mutable and scope.strip().lower() == "global":
        raise IntakeRejected(
            "mutable operational state cannot use global scope; use user or "
            "project:<slug> scope and supply valid_until when known.",
            rule="mutable_state_scope",
            reason="global_mutable_state",
        )
    return decision


def resolve_supersession_target(
    store: Any,
    claim_id: int | None,
    *,
    tenant_id: str | None,
    scope: str,
    visibility: str,
    source_agent: str | None,
) -> Claim | None:
    """Resolve a proposed replacement, flagging one from another identity.

    Returns ``(target, cross_identity)``. A mismatch on scope, visibility or
    source_agent used to raise, which failed the whole ingest and discarded the
    correction. That check protected nothing: same-identity supersession does
    not retire the target either — it files a proposal and a human decides — so
    nobody could unilaterally retire another agent's claim with or without it.
    All it decided was whether a correction was recorded or thrown away.

    The cost of throwing it away was a class of uncorrectable claims. A false
    claim written at scope ``global`` by an automated worker can never be
    matched by a session agent's source_agent, so no correction could ever be
    filed against it. On the production database in 2026-08 that was 26 of the
    133 confirmed claims at ``global``.

    Existence and liveness are still enforced: an unknown or already-retired
    target raises, whoever asks.
    """
    if claim_id is None:
        return None, False
    if isinstance(claim_id, bool) or not isinstance(claim_id, int) or claim_id <= 0:
        raise ValueError("supersession target must be a positive claim id")
    target = store.get_claim(claim_id, include_citations=False)
    if target is None or (tenant_id is not None and target.tenant_id != tenant_id):
        raise ValueError(f"supersession target {claim_id} does not exist")
    if target.status in {"archived", "superseded"}:
        raise ValueError("supersession target is not active")
    cross_identity = (target.scope, target.visibility, target.source_agent) != (
        scope,
        visibility,
        source_agent,
    )
    return target, cross_identity


def _as_reference(entry: str | int) -> Reference:
    if isinstance(entry, bool) or not isinstance(entry, (str, int)):
        raise ValueError("supersedes entries must be a claim id or a human id such as mm-a3f8")
    raw = str(entry).strip().lower()
    if raw.isdigit() and int(raw) > 0:
        return Reference("claim_id", int(raw))
    if raw.startswith("mm-") and len(raw) > 3:
        return Reference("human_id", raw)
    raise ValueError(f"supersedes entry {str(entry)[:40]!r} is not a claim id or a human id such as mm-a3f8")


def resolve_scoped_reference(
    store: Any,
    reference: Reference,
    *,
    tenant_id: str | None,
    scope: str,
    visibility: str,
    source_agent: str | None,
) -> Claim | None:
    """The claim a reference names INSIDE the replacement's tenant and scope, else None.

    A human id is only unique per scope, and a numeric id from another scope is out of bounds here: a correction
    never reaches across scopes. A private claim of another agent does not exist as far as the caller can tell.
    """
    if reference.kind == "human_id":
        target = store.get_claim_by_human_id(
            str(reference.value), include_citations=False, tenant_id=tenant_id, scope=scope,
            visibility=visibility, source_agent=source_agent,
        )
    else:
        target = store.get_claim(int(reference.value), include_citations=False)
    if target is None or target.tenant_id != tenant_id or target.scope != scope:
        return None
    if target.visibility != "public" and (target.visibility, target.source_agent) != (visibility, source_agent):
        return None
    return target


def resolve_explicit_supersedes(
    store: Any,
    supersedes: Sequence[str | int] | str | int | None,
    *,
    tenant_id: str | None,
    scope: str,
    visibility: str,
    source_agent: str | None,
) -> list[Claim]:
    """Resolve the structured ``supersedes`` argument (claim ids and/or human ids, N -> 1) or raise ValueError.

    Refused, before anything is written: an unknown reference, a claim in another tenant/scope, an already
    retired claim. Nothing is applied here; the caller files one steward proposal per target.
    """
    if supersedes is None or supersedes == "":
        return []
    entries = [supersedes] if isinstance(supersedes, (str, int)) else list(supersedes)
    if len(entries) > MAX_SUPERSEDES:
        raise ValueError(f"supersedes accepts at most {MAX_SUPERSEDES} claims")
    resolved: list[Claim] = []
    for entry in entries:
        reference = _as_reference(entry)
        target = resolve_scoped_reference(
            store, reference, tenant_id=tenant_id, scope=scope, visibility=visibility, source_agent=source_agent
        )
        if target is None:
            raise ValueError(f"supersession target {reference.value} does not exist in scope {scope!r}")
        if target.status in INACTIVE_STATUSES:
            raise ValueError("supersession target is not active")
        if all(target.id != known.id for known in resolved):
            resolved.append(target)
    return resolved


def _is_proposal(event: Any) -> bool:
    return str(getattr(event, "details", "") or "") == PROPOSAL_DETAILS


def has_live_proposal(store: Any, target_id: int, replacement_id: int | None = None) -> bool:
    """An unresolved supersession proposal exists for the target (resolved or rejected ones do not count).

    Mirrors ``recall.retrieval.pending_supersession_ids``: an approval that failed to apply does not resolve, and
    a proposal whose replacement is not ``confirmed`` does not hold the slot against ANOTHER replacement. Such a
    replacement may never become deliverable (too short for the validator, archived), and it used to block the
    next, honest correction of the same target for good, leaving old and new both served. A proposal already filed
    by ``replacement_id`` itself always counts (declared + named in prose is one proposal).
    """
    resolved: set[int] = set()
    for event in store.list_events(claim_id=target_id, event_type="audit", limit=200):
        details = str(getattr(event, "details", "") or "")
        if details not in {"steward_proposal_approved", "steward_proposal_rejected"}:
            continue
        try:
            payload = json.loads(getattr(event, "payload_json", None) or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or (details.endswith("approved") and payload.get("applied") is False):
            continue
        if isinstance(payload.get("proposal_event_id"), int):
            resolved.add(payload["proposal_event_id"])
    for event in store.list_events(claim_id=target_id, event_type="policy_decision", limit=100):
        if not _is_proposal(event) or int(event.id) in resolved:
            continue
        try:
            payload = json.loads(getattr(event, "payload_json", None) or "{}")
        except (ValueError, TypeError):
            payload = {}
        filed_by = payload.get("replaced_by_claim_id") if isinstance(payload, dict) else None
        if not isinstance(filed_by, int) or filed_by == replacement_id:
            return True
        replacement = store.get_claim(filed_by, include_citations=False)
        if replacement is not None and replacement.status == "confirmed":
            return True
    return False


def enforce_daily_bound(store: Any, source_agent: str | None, new_targets: int) -> None:
    """Refuse a declared supersession past the per-agent daily bound (raises ValueError, nothing written)."""
    agent = str(source_agent or "")
    since = window_start(1)
    filed = 0
    for event in store.list_events(event_type="policy_decision", limit=PROPOSAL_SCAN_CEILING, since=since):
        if not _is_proposal(event):
            continue
        try:
            payload = json.loads(getattr(event, "payload_json", None) or "{}")
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("source") == SOURCE_DIRECT_INGEST and payload.get("filed_by") == agent:
            filed += 1
    if filed + new_targets > MAX_DECLARED_PER_AGENT_PER_DAY:
        raise ValueError(
            f"supersedes limit reached: {filed} declared supersessions filed by this agent in the last 24h "
            f"(max {MAX_DECLARED_PER_AGENT_PER_DAY}); ask the operator to review the queue"
        )


def record_mutable_override(
    store: Any,
    claim: Claim,
    decision: MutableStateDecision,
    requested_volatility: str,
) -> None:
    if not decision.forced_high:
        return
    store.record_event(
        claim_id=claim.id,
        event_type="policy_decision",
        details="mutable_state:forced_high_volatility",
        payload={
            "rule": "mutable_state_expiry",
            "requested_volatility": requested_volatility,
            "effective_volatility": "high",
            "reason": "structured_valid_until_missing",
        },
    )


def apply_post_ingest_governance(
    store: Any,
    claim: Claim,
    decision: IngestGovernanceDecision,
    confidence: float,
) -> list[int]:
    """Record the mutable-state override and file supersession proposals; returns the target ids filed."""
    record_mutable_override(
        store,
        claim,
        decision.mutable_state,
        decision.requested_volatility,
    )
    filed = queue_supersession_proposals(store, decision.supersession_targets, claim, confidence)
    return filed + queue_prose_corrections(store, claim, confidence)


def _finish_supersession(
    service: Any, filed: list[int], governance: IngestGovernanceDecision, report: dict[str, Any] | None
) -> None:
    """Drop the service's cached pending set when a proposal was filed; say what happened to each declared target.

    A proposal filed now ranks in the very next recall of THIS service; another long-lived process (an MCP server)
    keeps its own cache for up to 300 s. ``report`` is the caller's ``supersession_report``.
    """
    if filed:
        service.__dict__.pop("_pending_supersession_cache", None)
    if report is not None:
        declared = [t.claim.id for t in governance.supersession_targets]
        report.update(
            declared=declared, filed=[i for i in declared if i in filed],
            not_filed=[i for i in declared if i not in filed],  # a live proposal for the target already exists
            prose_filed=[i for i in filed if i not in declared],
        )


def queue_for_revived(
    service: Any, governance: IngestGovernanceDecision, revived: Claim, confidence: float, report: dict[str, Any] | None
) -> None:
    """Dedup-hit path: the matched claim is the replacement; file the declared proposals against it."""
    filed = queue_supersession_proposals(service.store, governance.supersession_targets, revived, confidence)
    _finish_supersession(service, filed, governance, report)


def govern_created(
    service: Any, claim: Claim, governance: IngestGovernanceDecision, confidence: float, report: dict[str, Any] | None
) -> None:
    """Post-create governance: mutable-state override, declared and prose-named supersession proposals."""
    _finish_supersession(service, apply_post_ingest_governance(service.store, claim, governance, confidence), governance, report)


def queue_supersession_proposals(
    store: Any,
    targets: Sequence[SupersessionTarget],
    replacement: Claim,
    confidence: float,
) -> list[int]:
    """File one proposal per target; returns the target ids actually filed (a live proposal already there files none)."""
    return [
        t.claim.id
        for t in targets
        if t.claim.id != replacement.id
        and queue_supersession_proposal(store, t.claim, replacement, confidence, cross_identity=t.cross_identity)
    ]


def queue_prose_corrections(store: Any, claim: Claim, confidence: float) -> list[int]:
    """Safety net: the text names an earlier claim id as corrected -> operator-only proposal, never applied.

    Why a proposal and not the supersession itself (core.promotion_policy): ``transition_claim`` retires or
    confirms without review only for an ``operator``/``agent`` actor, i.e. an explicit decision by an authenticated
    caller. Ingest cannot authenticate its author (``source_agent`` is self-declared) and a regex over prose is an
    inference, not a decision, so no ingest path qualifies. The proposal carries source ``prose-correction``, which
    ``steward.OPERATOR_ONLY_SOURCES`` keeps away from the curation drain and every other automation. Meanwhile
    recall demotes the target (``pending_supersession_ids``; the prompt hook does not inject it); it is not retired.
    Never raises: a fault here must not fail the ingest that already persisted the claim.
    """
    try:
        filed: list[int] = []
        for reference in find_corrected_references(claim.text):
            target = resolve_scoped_reference(
                store, reference, tenant_id=claim.tenant_id, scope=claim.scope,
                visibility=claim.visibility, source_agent=claim.source_agent,
            )
            if target is None or target.id >= claim.id or target.status in INACTIVE_STATUSES:
                continue
            if queue_supersession_proposal(
                store, target, claim, confidence, source=SOURCE_PROSE_CORRECTION,
                cross_identity=target.source_agent != claim.source_agent,
            ):
                filed.append(target.id)
        return filed
    except Exception:  # noqa: BLE001 - see docstring
        logger.warning("prose correction scan failed for claim %s", claim.id, exc_info=True)
        return []


def queue_supersession_proposal(
    store: Any,
    target: Claim | None,
    replacement: Claim,
    confidence: float,
    *,
    cross_identity: bool = False,
    source: str = SOURCE_DIRECT_INGEST,
) -> int:
    """Queue steward review; direct ingest never rewrites confirmed truth.

    ``cross_identity`` marks a correction filed against a claim belonging to
    another scope, visibility or agent. It changes nothing about what happens
    here — the same proposal, the same human decision — but a reviewer needs to
    see it, since the reviewer cannot otherwise tell the correction came from
    somewhere with no standing over the target.

    Recording a proposal is not the same as it being acted on. When this was
    written the queue held 220 unresolved proposals, the oldest from
    2026-04-22, and the only resolutions on record came from one manual batch.
    """
    if target is None:
        return 0
    # Dedup on the TARGET, not on the (target, replacement) pair, but only against a LIVE proposal. A claim can
    # only be superseded once: whichever replacement is approved first retires it, and every further proposal
    # against the same target is then guaranteed to fail with "already superseded" (two dream-worker proposals
    # filed one second apart against claim 123737 latched into an unrecoverable state). A RESOLVED or rejected
    # proposal must not block a new one: a junk proposal that was rejected would otherwise stop the real
    # correction forever.
    if has_live_proposal(store, target.id, replacement.id):
        return 0
    store.record_event(
        claim_id=target.id,
        event_type="policy_decision",
        from_status=target.status,
        to_status="superseded",
        details=PROPOSAL_DETAILS,
        payload={
            "source": source,
            "filed_by": replacement.source_agent or "",
            "proposal_type": "review_queue_item",
            "decision": "superseded_candidate",
            "proposed_status": "superseded",
            "priority": confidence,
            "apply_requested": False,
            "cross_identity": cross_identity,
            "reasons": [
                {
                    "code": "prose_correction_text",
                    "detail": "The replacement's text names this claim id as corrected; inferred, not declared.",
                }
                if source == SOURCE_PROSE_CORRECTION
                else {
                    "code": "explicit_supersession_intent",
                    "detail": "Caller identified this candidate as a replacement.",
                },
                *(
                    [{
                        "code": "cross_identity_supersession",
                        "detail": (
                            f"Filed from scope {replacement.scope!r} by "
                            f"{replacement.source_agent!r} against a target in "
                            f"{target.scope!r} by {target.source_agent!r}. The "
                            "filer has no standing over the target; a reviewer "
                            "decides."
                        ),
                    }]
                    if cross_identity
                    else []
                ),
            ],
            "replaced_by_claim_id": replacement.id,
            "candidate_id": f"ingest:{replacement.id}",
        },
    )
    return 1


def validate_now(service: Any, claim_id: int) -> dict[str, object]:
    """Ingest fast path (ROADMAP 1.4; audit D-2/D-3): rule on ONE fresh candidate now, not at the next cycle.

    It runs the cycle's own stages for that claim, in the cycle's order: the deterministic format checks (a hard
    fail is ``conflicted``), the near-duplicate decision, the validator's gates (source and structure policy,
    pending supersession, tuple conflict, classifier or score) and, once the claim is confirmed, the curation
    drain's rule for the supersessions it declared (same-agent only; cross-identity and prose stay with the
    operator). No new promotion rule lives here. A near-duplicate is left a candidate whatever this process's
    dedupe flags say (``held: near_duplicate``): only candidates are ever deduped, so the cycle has to rule on it.
    Callers are the surfaces where an agent states a fact on purpose (MCP ``ingest_claim`` / ``checkpoint``);
    background intake keeps the steward cadence. Never raises: a fault leaves the claim a candidate for the cycle.
    ``MEMORYMASTER_INLINE_VALIDATION=0`` turns it off.
    """
    import contextlib
    import os

    if os.environ.get("MEMORYMASTER_INLINE_VALIDATION", "1").strip().lower() in {"0", "false", "off", "no"}:
        return {"enabled": False}
    from memorymaster.govern import candidate_dedupe
    from memorymaster.govern.jobs import curation_drain, deterministic, validator

    store = service.store
    report: dict[str, object] = {"enabled": True, "superseded": []}
    try:
        deterministic.run(store, workspace_root=service.workspace_root, limit=1, claim_ids=[claim_id])
        claim = store.get_claim(claim_id, include_citations=False)
        if claim is not None and claim.status == "candidate" and claim.text and claim.scope:
            with contextlib.closing(store.connect()) as conn:
                duplicate = candidate_dedupe.find_near_duplicate(
                    conn, candidate_id=claim.id, candidate_text=claim.text, candidate_scope=claim.scope
                )
            if duplicate.action == "archive":
                report["held"] = "near_duplicate"
            else:
                validator.run(store, limit=1, claim_ids=[claim_id])
        claim = store.get_claim(claim_id, include_citations=False)
        if claim is not None and claim.status == "confirmed":
            drained = curation_drain.drain_proposals(service, apply=True, replacement_claim_id=claim_id)
            report["superseded"] = drained["applied_targets"]
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.warning("inline validation of claim %s failed: %s", claim_id, type(exc).__name__, exc_info=True)
        report["error"] = type(exc).__name__
    # the replacement's status decides whether its target is hidden (recall.retrieval.pending_supersession_ids)
    service.__dict__.pop("_pending_supersession_cache", None)
    claim = store.get_claim(claim_id, include_citations=False)
    report["status"] = claim.status if claim is not None else None
    return report
