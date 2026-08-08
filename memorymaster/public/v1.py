"""Versioned remember/recall/forget/improve facade over governed MemoryMaster behavior."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from memorymaster.capture import CaptureEnvelope, CaptureRepository, capture_input
from memorymaster.capture.producers import ProducerItem, normalize_producer_item
from memorymaster.core.models import Claim, EvidenceItem, SourceItem
from memorymaster.core.scope_utils import scope_from_cwd
from memorymaster.core.session_scope import ResolvedScope, SessionScopeResolver
from memorymaster.core.service import MemoryService
from memorymaster.knowledge.context_bundle import query_context_bundle

API_VERSION = "memorymaster.public.v1"


@dataclass(frozen=True, slots=True)
class RememberReceipt:
    api_version: str
    source_item: dict[str, Any]
    evidence: dict[str, Any] | None
    job_ids: tuple[int, ...]
    deduplicated: bool
    warnings: tuple[str, ...]
    scope: str = "user"
    scope_source: str = "default_user"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RecallReceipt:
    api_version: str
    output: str
    claims: tuple[dict[str, Any], ...]
    token_budget: int
    tokens_used: int
    trust_mode: str
    output_format: str
    skills: tuple[dict[str, Any], ...] = ()
    scope: str = "user"
    scope_source: str = "default_user"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ForgetReceipt:
    api_version: str
    target: dict[str, Any]
    apply: bool
    actions: tuple[dict[str, Any], ...]
    evidence_preserved: bool = True
    privacy_erasure: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ImproveReceipt:
    api_version: str
    scope: str
    queued: dict[str, int]
    already_pending: dict[str, int]
    steward_review_due: int
    scope_source: str = "default_user"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _workspace_path(workspace: str | Path | None) -> Path | None:
    if workspace is None:
        configured = os.environ.get("MEMORYMASTER_WORKSPACE", "").strip()
        return Path(configured).resolve() if configured else Path.cwd().resolve()
    value = str(workspace).strip()
    return Path(value).resolve() if value else None


def _scope(scope: str | None, workspace: Path | None) -> str:
    if scope and scope.strip():
        return scope.strip()
    derived = scope_from_cwd(workspace)
    return derived if derived != "global" else "user"


def _service(db: str | Path | None, workspace: Path | None) -> MemoryService:
    target = str(
        db
        or os.environ.get("MEMORYMASTER_DB", "").strip()
        or os.environ.get("MEMORYMASTER_DEFAULT_DB", "").strip()
        or "memorymaster.db"
    )
    service = MemoryService(target, workspace_root=workspace or Path.cwd())
    service.init_db()
    return service


def _resolve_scope(
    service: MemoryService,
    *,
    scope: str | None,
    workspace: Path | None,
    session_id: str | None,
    source_agent: str,
    platform: str,
) -> ResolvedScope:
    return SessionScopeResolver(service.store.db_path).resolve(
        session_id=session_id,
        explicit_scope=scope,
        workspace=workspace,
        source_agent=source_agent,
        platform=platform,
    )


def _source_dict(source: SourceItem) -> dict[str, Any]:
    return {
        "id": source.id,
        "source_id": source.source_id,
        "source_item_id": source.source_item_id,
        "item_type": source.item_type,
        "content_hash": source.content_hash,
        "retired_at": source.retired_at,
        "retirement_reason": source.retirement_reason,
    }


def _evidence_dict(evidence: EvidenceItem) -> dict[str, Any]:
    return {
        "id": evidence.id,
        "source_item_id": evidence.source_item_id,
        "evidence_type": evidence.evidence_type,
        "provider": evidence.provider,
        "confidence": evidence.confidence,
        "content_hash": evidence.content_hash,
    }


def _persist_capture(
    service: MemoryService,
    *,
    envelope: Any,
    scope: str,
    source_agent: str,
) -> tuple[SourceItem, EvidenceItem | None, bool]:
    source = service.upsert_external_source(
        source_type="universal_capture",
        display_name="MemoryMaster Capture",
        config_json={"contract": API_VERSION},
    )
    producer = getattr(envelope, "producer", None)
    external_hash = getattr(envelope, "producer_external_id_hash", None)
    source_key = (
        f"producer:{producer}:{external_hash}"
        if producer and external_hash
        else envelope.locator if envelope.source_kind != "inline" else envelope.content_hash
    )
    existing_source = service.get_source_item(
        source_id=source.id, source_item_id=source_key
    )
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id=source_key,
        item_type=envelope.content_type,
        text=envelope.text,
        payload_json={
            "locator": envelope.locator,
            "mime_type": envelope.mime_type,
            "source_uri": envelope.source_uri,
            "scope": scope,
            "source_agent": source_agent,
            "provider_kind": envelope.provider_kind,
            "producer": producer,
            "producer_external_id_hash": external_hash,
            "producer_session_hash": getattr(envelope, "producer_session_hash", None),
            "producer_turn_id": getattr(envelope, "producer_turn_id", None),
            "producer_metadata": dict(getattr(envelope, "producer_metadata", ())),
        },
        content_hash=envelope.content_hash,
    )
    repository = CaptureRepository(service.store)
    evidence = repository.evidence_for_content(
        source_item_id=item.id, content_hash=envelope.content_hash
    )
    deduplicated = evidence is not None or (
        existing_source is not None and envelope.text is None
    )
    if evidence is None and envelope.text is not None:
        evidence = service.add_evidence_item(
            source_item_id=item.id,
            evidence_type=envelope.evidence_type or "text",
            text=envelope.text,
            provider="memorymaster-capture",
            confidence=1.0,
            payload_json={
                "locator": envelope.locator,
                "mime_type": envelope.mime_type,
                "producer": producer,
                "producer_external_id_hash": external_hash,
                "producer_session_hash": getattr(envelope, "producer_session_hash", None),
                "producer_turn_id": getattr(envelope, "producer_turn_id", None),
                "producer_metadata": dict(getattr(envelope, "producer_metadata", ())),
            },
            content_hash=envelope.content_hash,
        )
    return item, evidence, deduplicated


def _queue_capture_jobs(
    repository: CaptureRepository,
    *,
    source_item_id: int,
    content_hash: str,
    has_evidence: bool,
    blocked_code: str | None,
) -> tuple[tuple[int, ...], bool]:
    jobs: list[int] = []
    all_existing = True
    if not has_evidence:
        status = "blocked" if blocked_code else "pending"
        job, created = repository.queue_job(
            source_item_id=source_item_id,
            content_hash=content_hash,
            stage="extract_text",
            status=status,
            error_code=blocked_code,
            error_detail="Producer must submit extracted evidence." if blocked_code else None,
        )
        jobs.append(job.id)
        all_existing &= not created
    else:
        job, created = repository.queue_job(
            source_item_id=source_item_id,
            content_hash=content_hash,
            stage="extract_claims",
        )
        jobs.append(job.id)
        all_existing &= not created
    return tuple(jobs), all_existing


def remember(
    *,
    text: str | None = None,
    path: str | Path | None = None,
    source_uri: str | None = None,
    scope: str | None = None,
    source_agent: str = "memorymaster-public",
    session_id: str | None = None,
    platform: str = "local",
    producer: str | None = None,
    producer_external_id: str | None = None,
    producer_content_hash: str | None = None,
    producer_session_hash: str | None = None,
    producer_turn_id: str | None = None,
    producer_metadata: dict[str, Any] | None = None,
    db: str | Path | None = None,
    workspace: str | Path | None = None,
) -> RememberReceipt:
    """Persist evidence synchronously and queue governed extraction work."""
    workspace_path = _workspace_path(workspace)
    envelope = _remember_envelope(
        text=text,
        path=path,
        source_uri=source_uri,
        producer=producer,
        producer_external_id=producer_external_id,
        producer_content_hash=producer_content_hash,
        producer_session_hash=producer_session_hash,
        producer_turn_id=producer_turn_id,
        producer_metadata=producer_metadata,
    )
    service = _service(db, workspace_path)
    resolved = _resolve_scope(
        service,
        scope=scope,
        workspace=workspace_path,
        session_id=session_id,
        source_agent=source_agent,
        platform=platform,
    )
    item, evidence, evidence_duplicate = _persist_capture(
        service,
        envelope=envelope,
        scope=resolved.scope,
        source_agent=source_agent,
    )
    jobs, jobs_duplicate = _queue_capture_jobs(
        CaptureRepository(service.store),
        source_item_id=item.id,
        content_hash=envelope.content_hash,
        has_evidence=evidence is not None,
        blocked_code=envelope.blocked_code,
    )
    warnings = tuple(
        dict.fromkeys(
            (*envelope.warning_codes, *((envelope.blocked_code,) if envelope.blocked_code else ()))
        )
    )
    return RememberReceipt(
        api_version=API_VERSION,
        source_item=_source_dict(item),
        evidence=_evidence_dict(evidence) if evidence else None,
        job_ids=jobs,
        deduplicated=evidence_duplicate and jobs_duplicate,
        warnings=warnings,
        scope=resolved.scope,
        scope_source=resolved.scope_source,
    )


def _remember_envelope(
    *,
    text: str | None,
    path: str | Path | None,
    source_uri: str | None,
    producer: str | None,
    producer_external_id: str | None,
    producer_content_hash: str | None,
    producer_session_hash: str | None,
    producer_turn_id: str | None,
    producer_metadata: dict[str, Any] | None,
) -> CaptureEnvelope:
    if not producer:
        return capture_input(text=text, path=path, source_uri=source_uri)
    if text is None or path is not None or not producer_external_id:
        raise ValueError("Producer capture requires text and producer_external_id; paths are forbidden.")
    return normalize_producer_item(
        producer,
        ProducerItem(
            external_id=producer_external_id,
            text=text,
            source_uri=source_uri,
            content_hash=producer_content_hash,
            session_hash=producer_session_hash,
            turn_id=producer_turn_id,
            metadata=producer_metadata,
        ),
    )


def _citation_dict(citation: Any) -> dict[str, Any]:
    return {
        "id": citation.id,
        "source": citation.source,
        "locator": citation.locator,
        "excerpt": citation.excerpt,
    }


def _recall_claim(row: dict[str, Any]) -> dict[str, Any]:
    claim = row["claim"]
    breakdown = row.get("breakdown")
    return {
        "claim_id": claim.id,
        "human_id": claim.human_id,
        "text": claim.text,
        "status": claim.status,
        "scope": claim.scope,
        "confidence": claim.confidence,
        "score": row.get("score"),
        "score_explanation": breakdown if isinstance(breakdown, dict) else {},
        "citations": [_citation_dict(citation) for citation in claim.citations],
    }


def recall(
    query: str,
    *,
    scope_allowlist: list[str] | tuple[str, ...] | None = None,
    token_budget: int = 4000,
    trust_mode: str = "trusted",
    output_format: str = "text",
    retrieval_mode: str = "hybrid",
    include_skills: bool = False,
    skill_limit: int = 3,
    session_id: str | None = None,
    source_agent: str = "memorymaster-public",
    platform: str = "local",
    db: str | Path | None = None,
    workspace: str | Path | None = None,
) -> RecallReceipt:
    """Return governed context and structured lifecycle/citation details."""
    workspace_path = _workspace_path(workspace)
    service = _service(db, workspace_path)
    if scope_allowlist:
        scopes = list(scope_allowlist)
        receipt_scope = scopes[0] if len(scopes) == 1 else "multiple"
        scope_source = "scope_allowlist"
    else:
        resolved = _resolve_scope(
            service,
            scope=None,
            workspace=workspace_path,
            session_id=session_id,
            source_agent=source_agent,
            platform=platform,
        )
        scopes = [resolved.scope]
        receipt_scope = resolved.scope
        scope_source = resolved.scope_source
    result = query_context_bundle(
        service,
        query,
        scope_allowlist=scopes,
        token_budget=token_budget,
        output_format=output_format,
        retrieval_mode=retrieval_mode,
        trust_mode=trust_mode,
        include_skills=include_skills,
        skill_limit=skill_limit,
    )
    claims = tuple(_recall_claim(row) for row in result.rows)
    return RecallReceipt(
        api_version=API_VERSION,
        output=result.output,
        claims=claims,
        token_budget=result.token_budget,
        tokens_used=result.tokens_used,
        trust_mode=trust_mode,
        output_format=result.output_format,
        skills=result.skills,
        scope=receipt_scope,
        scope_source=scope_source,
    )


def _forget_claim_action(claim: Claim) -> dict[str, Any]:
    return {
        "claim_id": claim.id,
        "from_status": claim.status,
        "to_status": "archived",
        "reason": "friendly_forget_claim",
    }


def _source_actions(repository: CaptureRepository, source_item_id: int) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = [
        {
            "source_item_id": source_item_id,
            "action": "retire_source",
            "reason": "friendly_forget_source",
        }
    ]
    for claim in repository.claims_for_source(source_item_id):
        other_count = repository.active_supporting_source_count(
            claim.id, excluding_source_item_id=source_item_id
        )
        if other_count:
            next_status = claim.status
        elif claim.status == "candidate":
            next_status = "archived"
        elif claim.status == "confirmed":
            next_status = "stale"
        else:
            next_status = claim.status
        actions.append(
            {
                "claim_id": claim.id,
                "from_status": claim.status,
                "to_status": next_status,
                "other_active_sources": other_count,
            }
        )
    return actions


def _apply_source_actions(
    service: MemoryService,
    repository: CaptureRepository,
    source_item_id: int,
    actions: list[dict[str, Any]],
) -> None:
    repository.retire_source(source_item_id, reason="friendly_forget_source")
    for action in actions[1:]:
        if action["from_status"] == action["to_status"]:
            continue
        claim = service.store.get_claim(int(action["claim_id"]), include_citations=False)
        if claim is None:
            continue
        service.store.apply_status_transition(
            claim,
            to_status=str(action["to_status"]),
            reason="sole active evidence source retired",
            event_type="transition",
        )


def forget(
    *,
    claim_id: int | None = None,
    source_item_id: int | None = None,
    apply: bool = False,
    db: str | Path | None = None,
    workspace: str | Path | None = None,
) -> ForgetReceipt:
    """Preview or apply logical retirement; this is not privacy erasure."""
    if (claim_id is None) == (source_item_id is None):
        raise ValueError("Provide exactly one claim_id or source_item_id.")
    service = _service(db, _workspace_path(workspace))
    repository = CaptureRepository(service.store)
    if claim_id is not None:
        claim = service.store.get_claim(claim_id, include_citations=False)
        if claim is None:
            raise KeyError(f"claim {claim_id} not found")
        action = _forget_claim_action(claim)
        if apply and claim.status != "archived":
            service.store.apply_status_transition(
                claim,
                to_status="archived",
                reason="friendly_forget_claim",
                event_type="transition",
            )
        return ForgetReceipt(
            API_VERSION, {"claim_id": claim_id}, apply, (action,)
        )
    assert source_item_id is not None
    source = service.get_source_item_by_id(source_item_id)
    if source is None:
        raise KeyError(f"source item {source_item_id} not found")
    actions = _source_actions(repository, source_item_id)
    if apply and source.retired_at is None:
        _apply_source_actions(service, repository, source_item_id, actions)
    return ForgetReceipt(
        API_VERSION, {"source_item_id": source_item_id}, apply, tuple(actions)
    )


def _queue_due_evidence(
    repository: CaptureRepository, *, scope: str, limit: int
) -> tuple[int, int]:
    queued = existing = 0
    for row in repository.due_evidence(scope=scope, limit=limit):
        digest = str(row["content_hash"] or row["source_hash"] or "")
        if len(digest) != 64:
            continue
        _, created = repository.queue_job(
            source_item_id=int(row["source_item_id"]),
            content_hash=digest,
            stage="extract_claims",
        )
        queued += int(created)
        existing += int(not created)
    return queued, existing


def _queue_due_graph(
    repository: CaptureRepository, *, scope: str, limit: int
) -> tuple[int, int]:
    queued = existing = 0
    for row in repository.due_confirmed_graph_claims(scope=scope, limit=limit):
        if bool(row.get("job_exists")):
            existing += 1
            continue
        _, created = repository.queue_job(
            source_item_id=int(row["source_item_id"]),
            content_hash=str(row["job_content_hash"]),
            stage="extract_graph",
        )
        queued += int(created)
        existing += int(not created)
    return queued, existing


def improve(
    *,
    scope: str | None = None,
    max_items: int = 200,
    session_id: str | None = None,
    source_agent: str = "memorymaster-public",
    platform: str = "local",
    db: str | Path | None = None,
    workspace: str | Path | None = None,
) -> ImproveReceipt:
    """Queue due work without directly confirming or rewriting claims."""
    if not 1 <= max_items <= 200:
        raise ValueError("max_items must be between 1 and 200.")
    workspace_path = _workspace_path(workspace)
    service = _service(db, workspace_path)
    resolved = _resolve_scope(
        service,
        scope=scope,
        workspace=workspace_path,
        session_id=session_id,
        source_agent=source_agent,
        platform=platform,
    )
    resolved_scope = resolved.scope
    repository = CaptureRepository(service.store)
    claim_queued, claim_existing = _queue_due_evidence(
        repository, scope=resolved_scope, limit=max_items
    )
    graph_queued, graph_existing = _queue_due_graph(
        repository, scope=resolved_scope, limit=max_items
    )
    candidates = service.store.list_claims(
        status="candidate",
        limit=max_items,
        include_archived=False,
        scope_allowlist=[resolved_scope],
    )
    return ImproveReceipt(
        api_version=API_VERSION,
        scope=resolved_scope,
        queued={"extract_claims": claim_queued, "extract_graph": graph_queued},
        already_pending={"extract_claims": claim_existing, "extract_graph": graph_existing},
        steward_review_due=len(candidates),
        scope_source=resolved.scope_source,
    )
