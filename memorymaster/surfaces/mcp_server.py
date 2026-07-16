from dataclasses import asdict, dataclass
from functools import wraps
import hashlib
import http.client
import inspect
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, TypeVar
from urllib.parse import urlparse

from memorymaster.core import observability
from memorymaster.core.access_control import (
    AuthMode,
    RequestContext,
    authorize_context_action,
    bind_request_context,
    current_request_context,
    resolve_request_context,
)
from memorymaster.surfaces import mcp_path_policy
from pydantic import BaseModel, ValidationError

from memorymaster.core.models import CitationInput
from memorymaster.core.scope_utils import canonicalize_slug
from memorymaster.core.security import (
    expand_secret_scan_variants,
    redact_text,
    resolve_allow_sensitive_access,
)
from memorymaster.core.service import MemoryService

logger = logging.getLogger(__name__)

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover
    FastMCP = None  # type: ignore


_DEFAULT_DB = "memorymaster.db"
_DEFAULT_WORKSPACE = "."
_MAX_TEXT_INPUT_CHARS = 10_000
_TEXT_LIMIT_FIELDS = {"text", "body", "content"}
_ModelT = TypeVar("_ModelT", bound=BaseModel)
_ENV_DEFAULT_DB = os.environ.get("MEMORYMASTER_DEFAULT_DB", "").strip()
_ENV_DEFAULT_WORKSPACE = os.environ.get("MEMORYMASTER_WORKSPACE", "").strip()
_ENV_DEFAULT_PROJECT_SCOPE = os.environ.get("MEMORYMASTER_DEFAULT_PROJECT_SCOPE", "").strip()
_ENV_QUERY_INCLUDE_LEGACY_PROJECT = (
    os.environ.get("MEMORYMASTER_QUERY_INCLUDE_LEGACY_PROJECT", "1").strip().lower() not in {"0", "false", "no"}
)
_DEFAULT_INGEST_RATE_LIMIT_PER_MIN = 60
_INGEST_RATE_LIMIT_ENV = "MM_INGEST_RATE_LIMIT_PER_MIN"
_ANONYMOUS_SOURCE_AGENT = "_anonymous"
# Reserved key for the GLOBAL (cross-agent) bucket. A leading NUL keeps it out
# of any attacker-chosen source_agent namespace (NUL can't reach here via MCP).
_GLOBAL_RATE_AGENT = "\x00_global"
# The global bucket caps AGGREGATE ingestion across all source_agents so an
# attacker can't bypass the per-agent limit by rotating source_agent values
# (which previously also grew _INGEST_RATE_BUCKETS without bound). Sized as a
# multiple of the per-agent limit so legitimate multi-agent fan-out isn't
# throttled by normal traffic. (audit: rate-limit-partition-key)
_GLOBAL_RATE_MULTIPLIER = 10
# Hard cap on distinct per-agent buckets retained in memory; oldest-refilled
# entries are evicted past this so a source_agent-rotation flood can't grow the
# dict without bound.
_MAX_RATE_BUCKETS = 4096
_INGEST_RATE_BUCKETS: dict[str, tuple[float, float]] = {}
_INGEST_RATE_BUCKETS_LOCK = threading.Lock()
_monotonic = time.monotonic
# Slug canonicalization (copy/channel-suffix regexes) now lives in
# core/scope_utils.canonicalize_slug; _canonicalize_slug below delegates to it.


class _ToolInput(BaseModel):
    class Config:
        extra = "forbid"


class IngestClaimInput(_ToolInput):
    text: str
    sources_json: str = "[]"
    db: str = "memorymaster.db"
    workspace: str = "."
    idempotency_key: str = ""
    claim_type: str = ""
    subject: str = ""
    predicate: str = ""
    object_value: str = ""
    scope: str = "project"
    volatility: str = "medium"
    confidence: float = 0.5
    event_time: str = ""
    valid_from: str = ""
    valid_until: str = ""
    source_agent: str = ""
    holder: str = ""


class QueryMetaDecisionsInput(_ToolInput):
    query: str
    claim_types: list[str] = ["decision", "architecture"]
    top_n: int = 20
    db: str = "memorymaster.db"
    workspace: str = "."


def _structured_error(error: str, code: str, field: str | None = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error": error, "code": code}
    if field is not None:
        payload["field"] = field
    payload.update(extra)
    return payload


def _validation_field(loc: Any) -> str:
    if isinstance(loc, (list, tuple)) and loc:
        return str(loc[0])
    return str(loc or "")


def _validate_tool_input(
    model_type: type[_ModelT],
    payload: _ModelT | dict[str, Any],
    *,
    allow_raw_dict: bool = True,
) -> _ModelT | dict[str, Any]:
    if isinstance(payload, model_type):
        model = payload
    elif isinstance(payload, dict):
        if not allow_raw_dict:
            return _structured_error(
                f"{model_type.__name__} must be a pydantic BaseModel instance.",
                "INVALID_INPUT",
                "request",
            )
        try:
            model = model_type(**payload)
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {}
            field = _validation_field(first.get("loc"))
            error_type = str(first.get("type", ""))
            if error_type in {"value_error.missing", "missing"}:
                return _structured_error(f"Missing required field: {field}", "MISSING_FIELD", field)
            return _structured_error(str(first.get("msg", "Invalid input.")), "VALIDATION_ERROR", field or None)
    else:
        return _structured_error(
            f"{model_type.__name__} must be a pydantic BaseModel instance.",
            "INVALID_INPUT",
            "request",
        )

    for field in _TEXT_LIMIT_FIELDS:
        value = getattr(model, field, None)
        if isinstance(value, str) and len(value) > _MAX_TEXT_INPUT_CHARS:
            return _structured_error(
                f"Input field '{field}' exceeds {_MAX_TEXT_INPUT_CHARS} characters.",
                "INPUT_TOO_LONG",
                field,
                max_length=_MAX_TEXT_INPUT_CHARS,
            )
    return model


def _sensitivity_scan_variants(text: str):
    """Yield encoded/normalized variants of ``text`` for the ingest guard.

    Thin wrapper over ``security.expand_secret_scan_variants`` so the MCP ingest
    guard and the storage-time chokepoint share a single decoder implementation
    (audit: ingest-encoded-secret).
    """
    yield from expand_secret_scan_variants(text)


def _sensitive_input_error(text: str, field: str = "text") -> dict[str, Any] | None:
    normalized_findings: list[str] = []
    for variant in _sensitivity_scan_variants(text):
        _, findings = redact_text(variant)
        for finding in findings:
            if finding not in normalized_findings:
                normalized_findings.append(finding)
    if not normalized_findings:
        return None
    observability.bump_claim_filtered_findings(normalized_findings)
    return _structured_error(
        (
            "Claim rejected: contains credentials or secrets "
            f"({', '.join(normalized_findings)}). Never ingest passwords, tokens, or keys."
        ),
        "SENSITIVE_INPUT",
        field,
        findings=normalized_findings,
    )


def _ingest_rate_limit_per_min() -> int:
    raw = os.getenv(_INGEST_RATE_LIMIT_ENV, str(_DEFAULT_INGEST_RATE_LIMIT_PER_MIN)).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_INGEST_RATE_LIMIT_PER_MIN


def _refill_bucket(key: str, limit: int, timestamp: float) -> float:
    """Return the refilled token count for ``key`` without committing it."""
    tokens, last_refill = _INGEST_RATE_BUCKETS.get(key, (float(limit), timestamp))
    elapsed = max(0.0, timestamp - last_refill)
    return min(float(limit), tokens + elapsed * (limit / 60.0))


def _evict_rate_buckets() -> None:
    """Bound _INGEST_RATE_BUCKETS so source_agent rotation can't grow it forever.

    Evicts the least-recently-refilled per-agent entries (the global bucket is
    always retained). Caller must hold _INGEST_RATE_BUCKETS_LOCK.
    """
    if len(_INGEST_RATE_BUCKETS) <= _MAX_RATE_BUCKETS:
        return
    evictable = [k for k in _INGEST_RATE_BUCKETS if k != _GLOBAL_RATE_AGENT]
    evictable.sort(key=lambda k: _INGEST_RATE_BUCKETS[k][1])
    for stale_key in evictable[: len(_INGEST_RATE_BUCKETS) - _MAX_RATE_BUCKETS]:
        del _INGEST_RATE_BUCKETS[stale_key]


def _check_ingest_rate_limit(
    source_agent: str, now: float | None = None, *, cost: float = 1.0
) -> dict[str, Any] | None:
    """Debit ``cost`` tokens from the per-agent + global buckets, or reject.

    ``cost`` is the number of claims this call is about to ingest — 1 for
    ``ingest_claim``, ``len(items)`` for a ``checkpoint`` batch. The "may I
    act?" gate stays ``tokens >= 1.0`` (so single-ingest burst is unchanged),
    but a batch debits its full cost, which can drive the bucket negative and
    throttle the agent until it refills. This caps a batch tool's SUSTAINED
    throughput at ``limit``/min instead of ``limit × batch_size``/min — a
    single 200-item checkpoint that once cost 1 token now costs 200
    (audit: checkpoint-rate-cost).
    """
    limit = _ingest_rate_limit_per_min()
    if limit == 0:
        return _check_durable_ingest_quota(source_agent, cost)

    timestamp = _monotonic() if now is None else now
    key = _empty_to_none(source_agent) or _ANONYMOUS_SOURCE_AGENT
    global_limit = limit * _GLOBAL_RATE_MULTIPLIER
    with _INGEST_RATE_BUCKETS_LOCK:
        agent_tokens = _refill_bucket(key, limit, timestamp)
        global_tokens = _refill_bucket(_GLOBAL_RATE_AGENT, global_limit, timestamp)

        # Reject if EITHER the per-agent or the aggregate (global) bucket is
        # exhausted. The global bucket caps aggregate ingestion so rotating
        # source_agent values can't bypass the limit (audit: rate-limit-key).
        for bucket_key, tokens, bucket_limit in (
            (key, agent_tokens, limit),
            (_GLOBAL_RATE_AGENT, global_tokens, global_limit),
        ):
            if tokens < 1.0:
                refill_rate = bucket_limit / 60.0
                retry_after_ms = max(1, int(((1.0 - tokens) / refill_rate) * 1000))
                _INGEST_RATE_BUCKETS[key] = (agent_tokens, timestamp)
                _INGEST_RATE_BUCKETS[_GLOBAL_RATE_AGENT] = (global_tokens, timestamp)
                _evict_rate_buckets()
                return _structured_error(
                    f"ingest_claim rate limit exceeded for source_agent '{key}'.",
                    "RATE_LIMITED",
                    "source_agent",
                    retry_after_ms=retry_after_ms,
                    source_agent=key,
                    limit_per_min=limit,
                )

        _INGEST_RATE_BUCKETS[key] = (agent_tokens - cost, timestamp)
        _INGEST_RATE_BUCKETS[_GLOBAL_RATE_AGENT] = (global_tokens - cost, timestamp)
        _evict_rate_buckets()
    return _check_durable_ingest_quota(source_agent, cost)


def _check_durable_ingest_quota(
    source_agent: str, cost: float
) -> dict[str, Any] | None:
    from memorymaster.core.usage_ledger import UsageQuotaExceeded, reserve_configured

    actor = _empty_to_none(source_agent) or _ANONYMOUS_SOURCE_AGENT
    try:
        reserve_configured(
            operation="ingest",
            provider="mcp",
            actor=actor,
            units=max(1, int(cost)),
        )
    except UsageQuotaExceeded as exc:
        return _structured_error(
            "durable MCP ingest quota exhausted",
            "RATE_LIMITED",
            "source_agent",
            source_agent=actor,
            partition=exc.partition,
            limit_per_window=exc.limit,
        )
    return None


def _resolve_db(db: str) -> str:
    raw = str(db or "").strip()
    if raw and (raw != _DEFAULT_DB or not _ENV_DEFAULT_DB):
        resolved = raw
    else:
        resolved = _ENV_DEFAULT_DB or _DEFAULT_DB
    # v3.19.0-H4: refuse caller-supplied db paths that aren't allowlisted.
    # No-op when MEMORYMASTER_MCP_DB_ALLOWLIST is unset (back-compat).
    # Bypassable via MEMORYMASTER_MCP_ADMIN_MODE=1.
    mcp_path_policy.validate_db_path(resolved, actor="mcp_caller")
    return resolved


def _resolve_workspace(workspace: str) -> str:
    raw = str(workspace or "").strip()
    if raw:
        resolved = raw
    else:
        resolved = _ENV_DEFAULT_WORKSPACE or _DEFAULT_WORKSPACE
    mcp_path_policy.validate_workspace_path(resolved, actor="mcp_caller")
    return resolved


# Per-process usage-telemetry sessions, one per DB path (fresh-eyes audit
# 2026-07-01): SessionTracker was runtime-dead — nothing ever called
# start_session or bound MemoryService.session_id, so get_usage_rollup's
# session half always returned []. The MCP server is long-lived per client,
# so one session per DB per process is the honest granularity.
_TELEMETRY_SESSION_IDS: dict[tuple[str, str, str], int] = {}


def _bind_telemetry_session(
    svc: MemoryService,
    db_path: str,
    principal: str = "mcp-session",
    tenant_id: str | None = None,
) -> None:
    """Best-effort: bind a usage-telemetry session to *svc*.

    Telemetry must never break a tool call — every failure is swallowed and
    the service simply stays unbound (counters still work via source_agent).
    """
    try:
        session_key = (db_path, principal, tenant_id or "")
        sid = _TELEMETRY_SESSION_IDS.get(session_key)
        if sid is None:
            from memorymaster.surfaces.session_tracker import SessionTracker

            sid = SessionTracker(db_path).start_session(principal)
            _TELEMETRY_SESSION_IDS[session_key] = sid
        svc.session_id = sid
        if not getattr(svc, "source_agent", None):
            svc.source_agent = principal
    except Exception:
        pass


def _service(db: str, workspace: str, *, read_only: bool = False) -> MemoryService:
    db_path = _resolve_db(db)
    workspace_path = _resolve_workspace(workspace)
    context = current_request_context()
    principal = context.principal if context is not None else "mcp-session"
    tenant_id = context.tenant_id if context is not None else None
    svc = MemoryService(
        db_target=db_path,
        workspace_root=Path(workspace_path),
        tenant_id=tenant_id,
        require_tenant=context is not None and context.mode is AuthMode.TEAM,
        principal=principal,
        allowed_scopes=context.allowed_scopes if context is not None else frozenset(),
        read_only=read_only,
    )
    if not read_only:
        _bind_telemetry_session(svc, db_path, principal, tenant_id)
    svc.source_agent = principal
    return svc


def _read_service(db: str, workspace: str) -> MemoryService:
    return _service(db, workspace, read_only=True)


def _usage_rollup(db: str) -> dict[str, Any]:
    """Build the usage-telemetry rollup payload.

    Returns the Prometheus-style metrics text (including the
    ``recalls_queried_total`` family) plus the active agent sessions. Session
    lookup is best-effort: a missing/empty DB yields an empty list rather than
    an error. Only aggregate counters and session metadata are exposed — never
    claim text — so this stays safe to surface over MCP.
    """
    from memorymaster.surfaces.session_tracker import SessionTracker

    sessions: list[dict[str, Any]] = []
    try:
        sessions = SessionTracker(_resolve_db(db)).get_active_sessions()
    except Exception:  # pragma: no cover - defensive, tracker is itself guarded
        sessions = []
    return {
        "ok": True,
        "recalls_queried_total": observability.metric_family_total("recalls_queried_total"),
        "metrics_text": observability.metrics_text(),
        "active_sessions": sessions,
    }


def _record_mcp_usage(
    db: str,
    *,
    tool_name: str,
    started: float,
    result_status: str,
) -> None:
    """Persist aggregate tool evidence without storing queries or result paths."""
    try:
        from datetime import datetime, timezone
        from memorymaster.surfaces.mcp_usage import insert

        context = current_request_context()
        insert(
            _resolve_db(db),
            {
                "tool_name": tool_name,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "latency_ms": max(0, int((time.perf_counter() - started) * 1000)),
                "tenant_id": getattr(context, "tenant_id", None),
                "result_status": result_status,
            },
        )
    except Exception as exc:  # pragma: no cover - telemetry must never break tools
        logger.debug("MCP usage telemetry unavailable for %s: %s", tool_name, exc)


def _empty_to_none(value: str) -> str | None:
    v = value.strip()
    return v if v else None


def _bounded_limit(value: int, *, maximum: int) -> int:
    """Clamp caller-controlled result sizes to a finite positive range."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 1
    return min(maximum, max(1, parsed))


def _parse_sources_json(sources_json: str) -> list[CitationInput]:
    if not sources_json.strip():
        return []
    try:
        raw_list = json.loads(sources_json)
    except json.JSONDecodeError as exc:
        raise ValueError("sources_json must be valid JSON array of strings") from exc
    if not isinstance(raw_list, list):
        raise ValueError("sources_json must be a JSON array of strings")
    items: list[CitationInput] = []
    for raw in raw_list:
        if not isinstance(raw, str):
            continue
        parts = [part.strip() for part in raw.split("|", 2)]
        source = parts[0] if parts else ""
        if not source:
            continue
        locator = parts[1] if len(parts) > 1 and parts[1] else None
        excerpt = parts[2] if len(parts) > 2 and parts[2] else None
        items.append(CitationInput(source=source, locator=locator, excerpt=excerpt))
    return items


def _claim_to_dict(claim) -> dict[str, Any]:
    data = asdict(claim)
    return data


def _parse_scope_allowlist(raw: str) -> list[str] | None:
    values = [part.strip() for part in str(raw or "").split(",") if part.strip()]
    return values or None


def _canonicalize_slug(dirname: str) -> str:
    """Canonicalize a workspace dirname into a stable project slug.

    Delegates to the shared :func:`memorymaster.core.scope_utils.canonicalize_slug`
    so the slug logic + the three regexes live in exactly one place. Behaviour is
    identical to the historical in-module implementation.
    """
    return canonicalize_slug(dirname)


def _project_scope(workspace: str) -> str:
    """Derive a project scope from a workspace path.

    Returns the canonical ``project:<slug>`` form by default. The legacy
    hash-suffix form ``project:<slug>:<sha1[:8]>`` is only used when the env
    var ``MEMORYMASTER_SCOPE_DISAMBIGUATE=1`` is set — that's the escape hatch
    for hosts that genuinely have two different workspaces with the same
    directory name. For the common case (one workspace per slug on a host),
    dropping the hash prevents scope fragmentation where CLI ingests write
    ``project:wezbridge`` and MCP ingests write ``project:wezbridge:a6a83c6a``
    and nobody finds each other.

    If no workspace context is available (no arg, no env, no implicit ``.``
    override) we return the literal ``"user"`` scope rather than polluting a
    project namespace — bare ``project`` was a design smell that accumulated
    673 ambient claims in the live DB by 2026-04-22.
    """
    if _ENV_DEFAULT_PROJECT_SCOPE:
        return _ENV_DEFAULT_PROJECT_SCOPE
    raw_arg = str(workspace or "").strip()
    if not raw_arg and not _ENV_DEFAULT_WORKSPACE:
        return "user"
    workspace_path = Path(_resolve_workspace(workspace)).resolve()
    slug = _canonicalize_slug(workspace_path.name)
    if os.getenv("MEMORYMASTER_SCOPE_DISAMBIGUATE", "").strip().lower() in ("1", "true", "yes"):
        digest = hashlib.sha1(str(workspace_path).lower().encode("utf-8")).hexdigest()[:8]
        return f"project:{slug}:{digest}"
    return f"project:{slug}"


def _effective_ingest_scope(scope: str, workspace: str) -> str:
    raw = (scope or "").strip()
    effective = _project_scope(workspace) if not raw or raw == "project" else raw
    context = current_request_context()
    if context is not None and context.mode is AuthMode.TEAM and effective not in context.allowed_scopes:
        raise PermissionError("Requested claim scope is outside the authenticated scope grant.")
    return effective


def _effective_scope_allowlist(raw: str, workspace: str) -> list[str] | None:
    parsed = _parse_scope_allowlist(raw)
    context = current_request_context()
    if context is not None and context.mode is AuthMode.TEAM:
        requested = parsed or list(context.allowed_scopes)
        scopes = [scope for scope in requested if scope in context.allowed_scopes]
        if not scopes:
            raise PermissionError("Requested scopes do not intersect the authenticated scope grant.")
    elif parsed:
        return parsed
    else:
        scopes = [_project_scope(workspace), "global"]
        if _ENV_QUERY_INCLUDE_LEGACY_PROJECT:
            scopes.append("project")
    seen: set[str] = set()
    deduped: list[str] = []
    for value in scopes:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _qdrant_query(query: str, db: str, workspace: str, limit: int) -> dict[str, Any]:
    """Reject the legacy raw-Qdrant retrieval entrypoint during quarantine."""
    del query, db, workspace, limit
    raise PermissionError(
        "Direct Qdrant retrieval is quarantined until the governed retrieval "
        "planner can enforce authoritative policy rehydration."
    )


def _checkpoint_batch(
    svc: MemoryService,
    items: list,
    *,
    default_scope: str,
    workspace: str,
    source_agent: str,
) -> dict[str, Any]:
    """Core batch-ingest loop for the ``checkpoint`` tool (plan 3.2b).

    Each item runs through the SAME per-item sensitivity filter + ``svc.ingest``
    as ``ingest_claim`` — this is a round-trip optimization, never a filter
    bypass. Returns a per-item summary so a partial batch never silently drops a
    claim (no silent-dropper): ingested ids, sensitive-skips, and per-index errors.
    """
    claim_ids: list[int] = []
    errors: list[dict[str, Any]] = []
    skipped_sensitive = 0
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append({"index": idx, "error": "item is not a JSON object"})
            continue
        text = str(item.get("text", "") or "").strip()
        if not text:
            errors.append({"index": idx, "error": "missing text"})
            continue
        # Sacred sensitivity filter — same firewall as ingest_claim, per item.
        if _sensitive_input_error(text) is not None:
            skipped_sensitive += 1
            errors.append({"index": idx, "error": "sensitive_input_blocked"})
            continue
        try:
            citations = _parse_sources_json(str(item.get("sources_json", "[]") or "[]"))
        except ValueError as exc:
            errors.append({"index": idx, "error": f"sources_json: {exc}"})
            continue
        item_scope = str(item.get("scope") or default_scope or "project")
        if not citations:
            citations = [CitationInput(source="mcp-session", locator=item_scope)]
        try:
            claim = svc.ingest(
                text=text,
                citations=citations,
                idempotency_key=_empty_to_none(str(item.get("idempotency_key", "") or "")),
                claim_type=_empty_to_none(str(item.get("claim_type", "") or "")),
                subject=_empty_to_none(str(item.get("subject", "") or "")),
                predicate=_empty_to_none(str(item.get("predicate", "") or "")),
                object_value=_empty_to_none(str(item.get("object_value", "") or "")),
                scope=_effective_ingest_scope(item_scope, workspace),
                volatility=str(item.get("volatility", "medium") or "medium"),
                confidence=float(item.get("confidence") or 0.5),
                event_time=_empty_to_none(str(item.get("event_time", "") or "")),
                valid_from=_empty_to_none(str(item.get("valid_from", "") or "")),
                valid_until=_empty_to_none(str(item.get("valid_until", "") or "")),
                # holder landed after checkpoint (fresh-eyes audit seam gap):
                # keep batch items at parity with ingest_claim's fields.
                holder=_empty_to_none(str(item.get("holder", "") or "")),
                source_agent=source_agent,
                require_source_agent=True,
            )
            claim_ids.append(claim.id)
        except (ValueError, TypeError) as exc:
            errors.append({"index": idx, "error": str(exc)})
    return {
        "ok": True,
        "ingested": len(claim_ids),
        "skipped_sensitive": skipped_sensitive,
        "errors": errors,
        "claim_ids": claim_ids,
    }


@dataclass(frozen=True, slots=True)
class McpToolPolicy:
    action: str
    team_enabled: bool = False


MCP_TOOL_POLICIES: dict[str, McpToolPolicy] = {
    "archive_by_source": McpToolPolicy("compact"),
    "checkpoint": McpToolPolicy("ingest"),
    "classify_query": McpToolPolicy("query", team_enabled=True),
    "compact_memory": McpToolPolicy("compact"),
    "entity_stats": McpToolPolicy("configure"),
    "extract_entities": McpToolPolicy("ingest"),
    "federated_query": McpToolPolicy("query"),
    "find_related_claims": McpToolPolicy("configure"),
    "get_usage_rollup": McpToolPolicy("query"),
    "ingest_claim": McpToolPolicy("ingest", team_enabled=True),
    "ingest_rule": McpToolPolicy("ingest"),
    "init_db": McpToolPolicy("configure"),
    "list_claims": McpToolPolicy("query", team_enabled=True),
    "list_events": McpToolPolicy("query"),
    "list_steward_proposals": McpToolPolicy("query"),
    "local_search": McpToolPolicy("query"),
    "open_dashboard": McpToolPolicy("query"),
    "pin_claim": McpToolPolicy("steward"),
    "quality_scores": McpToolPolicy("steward"),
    "query_claim_paths": McpToolPolicy("query"),
    "query_for_context": McpToolPolicy("query"),
    "query_for_task": McpToolPolicy("query"),
    "query_memory": McpToolPolicy("query", team_enabled=True),
    "query_meta_decisions": McpToolPolicy("query"),
    "query_rules": McpToolPolicy("query"),
    "read_active_tasks": McpToolPolicy("query"),
    "recall_analysis": McpToolPolicy("query"),
    "recompute_tiers": McpToolPolicy("steward"),
    "redact_claim_payload": McpToolPolicy("delete"),
    "resolve_project": McpToolPolicy("ingest"),
    "resolve_steward_proposal": McpToolPolicy("steward"),
    "rules_export": McpToolPolicy("export"),
    "run_cycle": McpToolPolicy("steward"),
    "run_steward": McpToolPolicy("steward"),
    "search_verbatim": McpToolPolicy("export"),
    "volunteer_context": McpToolPolicy("query"),
}


def _same_configured_location(left: str, right: str) -> bool:
    if "://" in left or "://" in right:
        return left == right
    return Path(left).resolve() == Path(right).resolve()


def _team_default_scope(context: RequestContext) -> str:
    workspace_scope = _project_scope(context.workspace)
    if workspace_scope in context.allowed_scopes:
        return workspace_scope
    project_scopes = [scope for scope in context.allowed_scopes if scope.startswith("project:")]
    if len(project_scopes) == 1:
        return project_scopes[0]
    raise PermissionError("Authenticated workspace has no unambiguous project scope.")


def _team_request_principal() -> str | None:
    context = current_request_context()
    if context is None or context.mode is not AuthMode.TEAM:
        return None
    return context.principal


def _normalize_team_arguments(
    bound: inspect.BoundArguments,
    context: RequestContext,
    tool_name: str,
) -> None:
    if "db" in bound.arguments:
        requested_db = str(bound.arguments["db"] or "")
        if requested_db not in {"", _DEFAULT_DB} and not _same_configured_location(requested_db, context.db_target):
            raise PermissionError("Caller-selected database is outside the authenticated context.")
        bound.arguments["db"] = context.db_target
    if "workspace" in bound.arguments:
        requested_workspace = str(bound.arguments["workspace"] or "")
        if requested_workspace not in {"", _DEFAULT_WORKSPACE} and not _same_configured_location(
            requested_workspace,
            context.workspace,
        ):
            raise PermissionError("Caller-selected workspace is outside the authenticated context.")
        bound.arguments["workspace"] = context.workspace
    default_scope = _team_default_scope(context)
    for field in ("scope", "current_scope", "project_scope"):
        if field not in bound.arguments:
            continue
        requested_scope = str(bound.arguments[field] or "").strip()
        effective_scope = default_scope if requested_scope in {"", "project"} else requested_scope
        if effective_scope not in context.allowed_scopes:
            raise PermissionError("Caller-selected scope is outside the authenticated context.")
        bound.arguments[field] = effective_scope
    if "scope_allowlist" in bound.arguments:
        requested_scopes = _parse_scope_allowlist(str(bound.arguments["scope_allowlist"] or ""))
        if requested_scopes and any(scope not in context.allowed_scopes for scope in requested_scopes):
            raise PermissionError("Caller scope allowlist exceeds authenticated scopes.")
        narrowed = list(requested_scopes) if requested_scopes else sorted(context.allowed_scopes)
        if not narrowed:
            raise PermissionError("Caller scope allowlist does not intersect authenticated scopes.")
        bound.arguments["scope_allowlist"] = ",".join(narrowed)
    for field in ("source_agent", "actor"):
        if field in bound.arguments:
            bound.arguments[field] = context.principal
    if tool_name == "query_memory" and (
        str(bound.arguments.get("retrieval_mode", "legacy")) != "legacy"
        or bool(bound.arguments.get("auto_classify", False))
    ):
        raise PermissionError("Semantic MCP retrieval remains disabled in team mode pending planner containment.")


def _authorized_tool_callable(func: Any, policy: McpToolPolicy) -> Any:
    call_signature = inspect.signature(func)

    @wraps(func)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        bound = call_signature.bind_partial(*args, **kwargs)
        bound.apply_defaults()
        context = resolve_request_context(
            db_target=str(bound.arguments.get("db", "") or ""),
            workspace=str(bound.arguments.get("workspace", "") or ""),
        )
        authorize_context_action(context, policy.action)
        if context.mode is AuthMode.TEAM:
            _normalize_team_arguments(bound, context, func.__name__)
        if context.mode is AuthMode.TEAM and not policy.team_enabled:
            raise PermissionError(
                f"MCP tool '{func.__name__}' is disabled in team mode until its scope contract is verified."
            )
        if bool(bound.arguments.get("allow_sensitive", False)) and not context.allow_sensitive:
            raise PermissionError("Authenticated MCP context does not allow sensitive-data access.")
        with bind_request_context(context):
            return func(*bound.args, **bound.kwargs)

    setattr(guarded, "__mcp_action__", policy.action)
    setattr(guarded, "__mcp_team_enabled__", policy.team_enabled)
    return guarded


if FastMCP is not None:
    class AuthorizedFastMCP(FastMCP):
        """FastMCP registration that cannot omit authorization metadata."""

        def tool(self, *args: Any, **kwargs: Any) -> Any:
            register = super().tool(*args, **kwargs)

            def decorator(func: Any) -> Any:
                policy = MCP_TOOL_POLICIES.get(func.__name__)
                if policy is None:
                    raise RuntimeError(f"MCP tool '{func.__name__}' has no authorization policy.")
                return register(_authorized_tool_callable(func, policy))

            return decorator

else:  # pragma: no cover - import fallback when MCP dependency is unavailable
    AuthorizedFastMCP = None  # type: ignore[misc,assignment]


if FastMCP is not None:
    mcp = AuthorizedFastMCP("memorymaster")

    @mcp.tool()
    def init_db(
        db: str = "memorymaster.db",
        workspace: str = ".",
    ) -> dict[str, Any]:
        """Initialize MemoryMaster database schema."""
        svc = _service(db, workspace)
        svc.init_db()
        return {"ok": True, "db": db}

    @mcp.tool()
    def ingest_claim(
        text: str,
        sources_json: str = "[]",
        db: str = "memorymaster.db",
        workspace: str = ".",
        idempotency_key: str = "",
        claim_type: str = "",
        subject: str = "",
        predicate: str = "",
        object_value: str = "",
        scope: str = "project",
        volatility: str = "medium",
        confidence: float = 0.5,
        event_time: str = "",
        valid_from: str = "",
        valid_until: str = "",
        source_agent: str = "",
        holder: str = "",
    ) -> dict[str, Any]:
        """
        Ingest a claim into memory.

        `sources_json` is a JSON array of `source|locator|excerpt` strings.
        `source_agent` identifies who created this claim (e.g. "claude-session", "codex-session").
        `holder` (takes_vs_facts): who holds this belief (e.g. a person/team/agent).
        Omit for holder-agnostic facts. The belief TYPE (take/fact/bet/hunch)
        rides on `claim_type`.
        Bi-temporal fields (ISO-8601 strings, all optional):
          - event_time: when the fact occurred in the real world
          - valid_from: start of the claim validity window
          - valid_until: end of the validity window (omit if still current)
        """
        request = _validate_tool_input(
            IngestClaimInput,
            {
                "text": text,
                "sources_json": sources_json,
                "db": db,
                "workspace": workspace,
                "idempotency_key": idempotency_key,
                "claim_type": claim_type,
                "subject": subject,
                "predicate": predicate,
                "object_value": object_value,
                "scope": scope,
                "volatility": volatility,
                "confidence": confidence,
                "event_time": event_time,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "source_agent": source_agent,
                "holder": holder,
            },
        )
        if isinstance(request, dict):
            return request

        rate_limit_error = _check_ingest_rate_limit(request.source_agent)
        if rate_limit_error is not None:
            return rate_limit_error

        sensitive_error = _sensitive_input_error(request.text)
        if sensitive_error is not None:
            return sensitive_error

        try:
            citations = _parse_sources_json(request.sources_json)
        except ValueError as exc:
            return _structured_error(str(exc), "VALIDATION_ERROR", "sources_json")
        if not citations:
            citations = [CitationInput(source="mcp-session", locator=request.scope or "project")]
        # Auto-detect source_agent if not provided
        effective_source = _empty_to_none(request.source_agent) or "mcp-session"
        svc = _service(request.db, request.workspace)
        try:
            claim = svc.ingest(
                text=request.text,
                citations=citations,
                idempotency_key=_empty_to_none(request.idempotency_key),
                claim_type=_empty_to_none(request.claim_type),
                subject=_empty_to_none(request.subject),
                predicate=_empty_to_none(request.predicate),
                object_value=_empty_to_none(request.object_value),
                scope=_effective_ingest_scope(request.scope, request.workspace),
                volatility=request.volatility,
                confidence=request.confidence,
                event_time=_empty_to_none(request.event_time),
                valid_from=_empty_to_none(request.valid_from),
                valid_until=_empty_to_none(request.valid_until),
                source_agent=effective_source,
                holder=_empty_to_none(request.holder),
                require_source_agent=True,
            )
        except ValueError as exc:
            return _structured_error(str(exc), "VALIDATION_ERROR", "text")
        # Log to vault chronicle + cross-source synthesis
        try:
            from memorymaster.knowledge.vault_log import log_ingest
            log_ingest(claim.id, claim.subject, claim.scope)
        except Exception as exc:
            logger.debug("Vault log failed: %s", exc)
        try:
            from memorymaster.knowledge.vault_synthesis import synthesize_on_ingest
            import os
            vault_dir = os.path.join(os.environ.get("MEMORYMASTER_WORKSPACE", "."), "obsidian-vault")
            if os.path.isdir(vault_dir):
                synthesize_on_ingest(_claim_to_dict(claim), vault_dir)
        except Exception as exc:
            logger.debug("Vault synthesis failed: %s", exc)
        # Create timeline entry (use service store connection for WAL mode)
        try:
            with svc.store.connect() as _conn:
                _conn.execute(
                    "INSERT OR IGNORE INTO timeline_entries (scope, subject, date, source, summary, claim_id) VALUES (?, ?, date('now'), ?, ?, ?)",
                    (claim.scope, claim.subject or "", effective_source, claim.text[:200], claim.id),
                )
        except Exception as exc:
            logger.debug("Timeline entry failed: %s", exc)

        return {"ok": True, "claim": _claim_to_dict(claim)}

    @mcp.tool()
    def checkpoint(
        claims_json: str,
        db: str = "memorymaster.db",
        workspace: str = ".",
        scope: str = "project",
        source_agent: str = "",
    ) -> dict[str, Any]:
        """Batch-ingest many claims in ONE call (session checkpoint, plan 3.2b).

        `claims_json` is a JSON array of objects, each with at least `text` and
        optionally: sources_json, claim_type, subject, predicate, object_value,
        scope, volatility, confidence, event_time, valid_from, valid_until.
        Every item passes through the SAME sensitivity filter + ingest path as
        ingest_claim — this only saves N round-trips, it does not bypass anything.
        Returns a per-item summary (ingested ids, sensitive-skips, per-index
        errors) so a partial batch never silently drops a claim.
        """
        import json as _json
        try:
            items = _json.loads(claims_json)
        except (ValueError, TypeError) as exc:
            return _structured_error(f"claims_json is not valid JSON: {exc}", "VALIDATION_ERROR", "claims_json")
        if not isinstance(items, list):
            return _structured_error("claims_json must be a JSON array", "VALIDATION_ERROR", "claims_json")
        if len(items) > 200:
            return _structured_error("checkpoint batch too large (max 200 items)", "VALIDATION_ERROR", "claims_json")
        if not items:
            return {"ok": True, "ingested": 0, "skipped_sensitive": 0, "errors": [], "claim_ids": []}
        effective_source = _empty_to_none(source_agent) or "mcp-session"
        # Charge the rate bucket per-item: a batch of N claims costs N tokens,
        # not 1 (audit: checkpoint-rate-cost — a 200-item batch previously
        # bypassed the per-agent ingest limit ~200x).
        rate_limit_error = _check_ingest_rate_limit(effective_source, cost=float(len(items)))
        if rate_limit_error is not None:
            return rate_limit_error
        svc = _service(db, workspace)
        return _checkpoint_batch(
            svc, items, default_scope=scope, workspace=workspace, source_agent=effective_source,
        )

    @mcp.tool()
    def archive_by_source(
        source_agent: str,
        db: str = "memorymaster.db",
        workspace: str = ".",
        dry_run: bool = True,
        limit: int = 0,
    ) -> dict[str, Any]:
        """Bulk-ARCHIVE (never delete) all live claims from one `source_agent`.

        Lifecycle-safe cleanup for eval/backfill pollution: matched claims move
        to status `archived` (MemoryMaster has no hard delete; claims terminate
        at archived). `dry_run=True` (default) only REPORTS what would be archived
        — call again with `dry_run=False` to apply. `limit=0` means no cap; when a
        cap truncates the match set the result carries `truncated=True`.
        Returns matched/archived counts + claim_ids.
        """
        if not source_agent or not source_agent.strip():
            return _structured_error("source_agent is required", "VALIDATION_ERROR", "source_agent")
        eff_limit = limit if limit and limit > 0 else None
        svc = _service(db, workspace)
        result = svc.store.archive_by_source(source_agent.strip(), dry_run=dry_run, limit=eff_limit)
        return {"ok": True, **result}

    @mcp.tool()
    def resolve_project(
        alias: str,
        remember: bool = False,
        db: str = "memorymaster.db",
        workspace: str = ".",
    ) -> dict[str, Any]:
        """Resolve a fuzzy project *alias* to canonical on-disk path(s).

        Memory-first, Everything-second resolution returning every candidate
        with an explainable confidence score and human-readable evidence. All
        returned paths are collapsed to root-relative tokens. The default is
        read-only; ``remember=True`` explicitly requests persistence of a
        high-confidence, non-sensitive match.
        """
        from memorymaster.bridges.local_search.everything import EverythingProvider
        from memorymaster.bridges.local_search.redact import collapse_path, load_roots
        from memorymaster.bridges.local_search.resolver import resolve_project as _resolve

        raw_alias = str(alias or "").strip()
        if not raw_alias:
            return _structured_error("alias must be a non-empty string", "VALIDATION_ERROR", "alias")
        try:
            svc = _service(db, workspace)
        except (ValueError, OSError) as exc:
            return _structured_error(str(exc), "VALIDATION_ERROR", "workspace")
        roots = load_roots()
        provider = EverythingProvider()
        started = time.perf_counter()
        result = _resolve(
            raw_alias,
            svc=svc,
            provider=provider,
            roots=roots,
            remember=bool(remember),
        )

        def _match_dict(match: Any) -> dict[str, Any]:
            return {
                "path": collapse_path(roots, match.path),
                "confidence": match.confidence,
                "evidence": list(match.evidence),
                "source": match.source,
            }

        payload = {
            "ok": True,
            "query": result.query,
            "canonical_slug": result.canonical_slug,
            "matches": [_match_dict(m) for m in result.matches],
            "best": _match_dict(result.best) if result.best is not None else None,
            "degraded": result.degraded,
            "remembered": result.remembered,
        }
        if result.degraded:
            status = "degraded"
        elif result.best is None:
            status = "no_match"
        elif result.remembered:
            status = "everything_match_remembered"
        else:
            status = f"{result.best.source}_match"
        _record_mcp_usage(
            db,
            tool_name="resolve_project",
            started=started,
            result_status=status,
        )
        return payload

    @mcp.tool()
    def local_search(
        query: str,
        limit: int = 50,
        kind: str = "any",
        exact: bool = False,
        db: str = "memorymaster.db",
        workspace: str = ".",
    ) -> dict[str, Any]:
        """Read-only path lookup across the machine via Everything (ES.exe).

        Thin wrapper over the local-search provider; performs no ingest. Output
        paths are collapsed to root-relative tokens so a tool result never
        prints a raw ``C:\\Users\\<name>`` path into a transcript. Returns
        ``degraded: true`` when the search backend is unavailable. Set
        ``exact=True`` for whole-name matching instead of substring matching.
        """
        from memorymaster.bridges.local_search.everything import EverythingProvider
        from memorymaster.bridges.local_search.redact import collapse_path, load_roots

        raw_query = str(query or "").strip()
        if not raw_query:
            return _structured_error("query must be a non-empty string", "VALIDATION_ERROR", "query")
        if kind not in ("any", "dir", "file"):
            return _structured_error("kind must be one of: any, dir, file", "VALIDATION_ERROR", "kind")
        safe_limit = max(1, min(int(limit), 1000))
        roots = load_roots()
        provider = EverythingProvider()
        started = time.perf_counter()
        degraded = not provider.available()
        hits = provider.search(
            raw_query,
            limit=safe_limit,
            kind=kind,
            whole_name=bool(exact),
        )
        rows = [
            {
                "path": collapse_path(roots, hit.path),
                "kind": hit.kind,
                "size": hit.size,
                "modified": hit.modified,
            }
            for hit in hits
        ]
        status = "degraded" if degraded else ("ok_hits" if rows else "ok_empty")
        _record_mcp_usage(
            db,
            tool_name=f"local_search:{'exact' if exact else 'fuzzy'}",
            started=started,
            result_status=status,
        )
        return {
            "ok": True,
            "hits": rows,
            "degraded": degraded,
            "exact": bool(exact),
        }

    @mcp.tool()
    def run_cycle(
        db: str = "memorymaster.db",
        workspace: str = ".",
        with_compact: bool = False,
        min_citations: int = 1,
        min_score: float = 0.58,
        policy_mode: str = "legacy",
        policy_limit: int = 200,
    ) -> dict[str, Any]:
        """Run one full maintenance cycle: extract, deterministic validate, validate, decay, compact(optional)."""
        svc = _service(db, workspace)
        result = svc.run_cycle(
            run_compactor=with_compact,
            min_citations=min_citations,
            min_score=min_score,
            policy_mode=policy_mode,
            policy_limit=policy_limit,
        )
        return {"ok": True, "result": result}

    @mcp.tool()
    def run_steward(
        db: str = "memorymaster.db",
        workspace: str = ".",
        mode: str = "manual",
        cadence_trigger: str = "timer",
        interval_seconds: float = 30.0,
        git_check_seconds: float = 10.0,
        commit_every: int = 1,
        max_cycles: int = 1,
        allow_sensitive: bool = False,
        apply: bool = False,
        max_claims: int = 200,
        max_proposals: int = 200,
        max_probe_files: int = 200,
        max_probe_file_bytes: int = 512 * 1024,
        max_tool_probes: int = 200,
        probe_timeout_seconds: float = 2.0,
        probe_failure_threshold: int = 3,
        enable_semantic_probe: bool = True,
        enable_tool_probe: bool = True,
        artifact_path: str = "artifacts/steward/steward_report.json",
    ) -> dict[str, Any]:
        """Run the stewardship loop and emit an audit report artifact."""
        from memorymaster.govern.steward import run_steward as _run_steward

        allow_sensitive = resolve_allow_sensitive_access(
            allow_sensitive=allow_sensitive,
            context="mcp.run_steward",
        )
        svc = _service(db, workspace)
        result = _run_steward(
            svc,
            mode=str(mode).strip().lower(),  # type: ignore[arg-type]
            cadence_trigger=str(cadence_trigger).strip().lower(),  # type: ignore[arg-type]
            interval_seconds=interval_seconds,
            git_check_seconds=git_check_seconds,
            commit_every=commit_every,
            max_cycles=max_cycles,
            allow_sensitive=allow_sensitive,
            apply=apply,
            max_claims=max_claims,
            max_proposals=max_proposals,
            max_probe_files=max_probe_files,
            max_probe_file_bytes=max_probe_file_bytes,
            max_tool_probes=max_tool_probes,
            probe_timeout_seconds=probe_timeout_seconds,
            probe_failure_threshold=probe_failure_threshold,
            enable_semantic_probe=enable_semantic_probe,
            enable_tool_probe=enable_tool_probe,
            artifact_path=artifact_path,
        )
        return {"ok": True, "result": result}

    @mcp.tool()
    def classify_query(query: str) -> dict[str, Any]:
        """Classify a query and report its recommended and effective modes."""
        from memorymaster.recall.query_classifier import classify_query as _classify, recommended_retrieval_mode

        qtype = _classify(query)
        recommended_mode = recommended_retrieval_mode(qtype)
        effective_mode = "legacy" if recommended_mode == "qdrant" else recommended_mode
        result = {
            "query_type": qtype,
            "recommended_mode": recommended_mode,
            "effective_mode": effective_mode,
        }
        if recommended_mode == "qdrant":
            result["containment_reason"] = (
                "qdrant retrieval is quarantined pending the governed retrieval planner"
            )
        return result

    def _apply_detail_level(claim_dict: dict[str, Any], detail_level: str) -> dict[str, Any]:
        """Filter claim dict fields based on requested detail level.

        - "summary": claim_id, human_id, status, confidence, score, text[:80]
        - "standard": full claim dict (current behaviour)
        - "full": full claim dict (citations already included by caller)
        """
        if detail_level == "summary":
            return {
                "claim_id": claim_dict.get("id"),
                "human_id": claim_dict.get("human_id"),
                "status": claim_dict.get("status"),
                "confidence": claim_dict.get("confidence"),
                "text": (claim_dict.get("text") or "")[:80],
            }
        # "standard" and "full" return the full dict — caller is responsible for
        # including citations when detail_level == "full".
        return claim_dict

    def _enrich_claims_with_citations(svc: Any, rows_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Enrich claims with citations for 'full' detail level."""
        enriched: list[dict[str, Any]] = []
        for row in rows_data:
            claim_obj = row["claim"]
            cid = getattr(claim_obj, "id", None)
            if cid is not None:
                full_claim = svc.store.get_claim(int(cid), include_citations=True)
                if full_claim is not None:
                    claim_obj = full_claim
            enriched.append({**row, "claim": claim_obj})
        return enriched

    @mcp.tool()
    def query_memory(
        query: str,
        db: str = "memorymaster.db",
        workspace: str = ".",
        limit: int = 20,
        retrieval_mode: str = "legacy",
        trust_mode: str = "trusted",
        auto_classify: bool = False,
        include_stale: bool | None = None,
        include_conflicted: bool | None = None,
        include_candidates: bool | None = None,
        allow_sensitive: bool = False,
        scope_allowlist: str = "",
        detail_level: str = "standard",
    ) -> dict[str, Any]:
        """Query governed memory; trusted mode returns confirmed claims only.

        retrieval_mode options:
          - "legacy" (default, fastest ~0.1s): SQL text search
          - "qdrant": temporarily quarantined; falls back to authoritative lexical search
          - "hybrid" (slow ~8s): local sentence-transformers vector + lexical ranking

        auto_classify: when True and retrieval_mode is "legacy", classify the query
          automatically and upgrade to the recommended retrieval mode.

        detail_level options:
          - "summary": claim_id, human_id, status, confidence, text[:80]
          - "standard" (default): full claim dict
          - "full": full claim dict with citations inlined
        """
        from memorymaster.recall.query_classifier import classify_query as _classify, recommended_retrieval_mode

        limit = _bounded_limit(limit, maximum=100)
        resolve_allow_sensitive_access(
            allow_sensitive=allow_sensitive,
            context="mcp.query_memory",
        )

        requested_retrieval_mode = retrieval_mode
        classified_retrieval_mode: str | None = None
        query_type: str | None = None
        if auto_classify and retrieval_mode == "legacy":
            query_type = _classify(query)
            classified_retrieval_mode = recommended_retrieval_mode(query_type)
            retrieval_mode = classified_retrieval_mode

        from memorymaster.recall.planner import RetrievalRequest
        svc = _read_service(db, workspace)
        retrieval = svc.retrieve(RetrievalRequest(
            query_text=query,
            limit=limit,
            trust_mode=trust_mode,
            retrieval_mode=retrieval_mode,
            include_stale=include_stale,
            include_conflicted=include_conflicted,
            include_candidates=include_candidates,
            allow_sensitive=allow_sensitive,
            scope_allowlist=tuple(_effective_scope_allowlist(scope_allowlist, workspace)),
            requesting_agent=_team_request_principal(),
            query_type=query_type,
            qdrant_candidate_reads=(
                retrieval_mode == "qdrant"
                and os.environ.get("MEMORYMASTER_QDRANT_GOVERNED_READS", "").strip().lower()
                in {"1", "true", "yes", "on"}
            ),
        ))
        rows_data = list(retrieval.rows)
        # For "full" detail level, re-fetch each claim with citations inline.
        if detail_level == "full":
            rows_data = _enrich_claims_with_citations(svc, rows_data)

        claims = [row["claim"] for row in rows_data]
        serialized_claims = [
            _apply_detail_level(_claim_to_dict(claim), detail_level)
            for claim in claims
        ]
        serialized_rows: list[dict[str, Any]] = []
        for index, row in enumerate(rows_data):
            serialized_rows.append(
                {
                    "claim_index": index,
                    "status": row.get("status"),
                    "annotation": row.get("annotation", {}),
                    "score": row.get("score", 0.0),
                    "lexical_score": row.get("lexical_score", 0.0),
                    "freshness_score": row.get("freshness_score", 0.0),
                    "confidence_score": row.get("confidence_score", 0.0),
                    "vector_score": row.get("vector_score", 0.0),
                    "tier": row["claim"].tier if hasattr(row["claim"], "tier") else "working",
                }
            )
        response: dict[str, Any] = {
            "ok": True,
            "rows": len(claims),
            "claims": serialized_claims,
            "rows_data": serialized_rows,
            "response_contract": "memorymaster.retrieval.v2",
            "trust_mode": retrieval.plan.trust_mode,
            "requested_retrieval_mode": requested_retrieval_mode,
            "retrieval_mode": retrieval.plan.effective_mode,
        }
        if query_type is not None:
            response["query_type"] = query_type
        if retrieval.plan.containment_reason is not None:
            response["containment_reason"] = retrieval.plan.containment_reason
            if classified_retrieval_mode is not None:
                response["classified_retrieval_mode"] = classified_retrieval_mode

        return response

    @mcp.tool()
    def query_claim_paths(
        claim_id: str,
        db: str = "memorymaster.db",
        workspace: str = ".",
        edge_type: str = "",
        direction: str = "both",
        max_hops: int = 2,
        include_stale: bool = False,
        include_conflicted: bool = False,
        scope_allowlist: str = "",
        allow_sensitive: bool = False,
    ) -> dict[str, Any]:
        """Traverse claim relationship paths from a starting claim (read-only).

        Answers relational questions over the ``claim_links`` graph:
          - provenance ("what led to X?")     → direction="in"
          - impact     ("what depends on X?") → direction="out"
          - conflict   ("what contradicts X?") → edge_type="contradicts"

        claim_id accepts a numeric id OR a human_id string.
        direction: "in" (incoming), "out" (outgoing), or "both" (default).
        edge_type: filter to one link type (empty = all types).
        max_hops: BFS depth, clamped server-side to a sane maximum.

        Each result row has: claim (full dict), depth, edge_chain (link types
        traversed), path (claim ids), and path_confidence (weakest-link =
        minimum claim confidence across the path). Orphaned claim → empty list.

        Like every other read tool, results are gated by ``scope_allowlist``
        (defaults to this workspace's project + global scope) and drop
        sensitive-visibility claims unless ``allow_sensitive`` is set — so a
        known claim_id can't leak cross-scope or sensitive claim text via graph
        traversal (audit: claim-paths-scope-gate).
        """
        svc = _read_service(db, workspace)
        normalized_scopes = _effective_scope_allowlist(scope_allowlist, workspace)
        allow_sensitive = resolve_allow_sensitive_access(
            allow_sensitive=allow_sensitive,
            context="mcp.query_claim_paths",
        )
        rows = svc.query_claim_paths(
            claim_id,
            edge_type=(edge_type.strip() or None),
            direction=direction,
            max_hops=max_hops,
            include_stale=include_stale,
            include_conflicted=include_conflicted,
            scope_allowlist=normalized_scopes,
            allow_sensitive=allow_sensitive,
            requesting_agent=os.getenv("MEMORYMASTER_SOURCE_AGENT") or None,
        )
        return {"ok": True, "rows": len(rows), "paths": rows}

    @mcp.tool()
    def recall_analysis(
        query: str,
        db: str = "memorymaster.db",
        workspace: str = ".",
        limit: int = 10,
        retrieval_mode: str = "hybrid",
        include_stale: bool = True,
        include_conflicted: bool = True,
        include_candidates: bool = True,
        retrieval_profile: str = "",
        allow_sensitive: bool = False,
        scope_allowlist: str = "",
    ) -> dict[str, Any]:
        """Explain WHY each claim ranked where it did (ranking introspection).

        Read-only observability tool. Returns, per claim, the full score
        breakdown — raw lexical/confidence/freshness/vector signals, the
        weighted contributions, tier and pinned bonuses, the relevance vs.
        boost subtotals, whether the floor-ratio gate suppressed the boosts,
        and the final score — plus the retrieval weights/profile actually in
        force and the per-component claim rankings.

        Use this to debug recall: why did a relevant claim rank low, or an
        off-topic one rank high? It does NOT change ranking — it surfaces the
        same numbers ``query_memory`` ranked on.
        """
        resolve_allow_sensitive_access(
            allow_sensitive=allow_sensitive,
            context="mcp.recall_analysis",
        )
        svc = _read_service(db, workspace)
        analysis = svc.recall_analysis(
            query_text=query,
            limit=limit,
            retrieval_mode=retrieval_mode,
            include_stale=include_stale,
            include_conflicted=include_conflicted,
            include_candidates=include_candidates,
            retrieval_profile=(retrieval_profile.strip() or None),
            allow_sensitive=allow_sensitive,
            scope_allowlist=_effective_scope_allowlist(scope_allowlist, workspace),
        )
        return {"ok": True, **analysis}

    @mcp.tool()
    def query_for_context(
        query: str,
        db: str = "memorymaster.db",
        workspace: str = ".",
        token_budget: int = 4000,
        output_format: str = "text",
        limit: int = 100,
        retrieval_mode: str = "legacy",
        trust_mode: str = "trusted",
        include_stale: bool | None = None,
        include_conflicted: bool | None = None,
        include_candidates: bool | None = None,
        allow_sensitive: bool = False,
        scope_allowlist: str = "",
        detail_level: str = "standard",
    ) -> dict[str, Any]:
        """Pack the most relevant claims into a token-budgeted context block.

        THE context window optimizer for AI agents. Returns a formatted text
        block (text, xml, or json) that fits within `token_budget` tokens,
        ranked by relevance using hybrid search (lexical + vector + freshness).

        Use this instead of query_memory when you need to inject memory
        directly into a system prompt or context window.

        detail_level options (applied to the structured claims list in the response):
          - "summary": claim_id, human_id, status, confidence, text[:80]
          - "standard" (default): full claim dict
          - "full": full claim dict with citations inlined
        """
        limit = _bounded_limit(limit, maximum=250)
        token_budget = _bounded_limit(token_budget, maximum=32_000)
        resolve_allow_sensitive_access(
            allow_sensitive=allow_sensitive,
            context="mcp.query_for_context",
        )
        svc = _read_service(db, workspace)
        result = svc.query_for_context(
            query=query,
            token_budget=token_budget,
            output_format=output_format,
            limit=limit,
            include_stale=include_stale,
            include_conflicted=include_conflicted,
            include_candidates=include_candidates,
            retrieval_mode=retrieval_mode,
            trust_mode=trust_mode,
            allow_sensitive=allow_sensitive,
            scope_allowlist=_effective_scope_allowlist(scope_allowlist, workspace),
        )
        response: dict[str, Any] = {
            "ok": True,
            "output": result.output,
            "claims_considered": result.claims_considered,
            "claims_included": result.claims_included,
            "tokens_used": result.tokens_used,
            "token_budget": result.token_budget,
            "format": result.format,
        }
        if detail_level != "standard":
            # Attach filtered claim list as a convenience for callers who want
            # structured data alongside the formatted output block.
            rows_data = list(result.rows)
            if detail_level == "full":
                filtered_claims = []
                for row in rows_data:
                    cid = getattr(row["claim"], "id", None)
                    claim_obj = row["claim"]
                    if cid is not None:
                        full_claim = svc.store.get_claim(int(cid), include_citations=True)
                        if full_claim is not None:
                            claim_obj = full_claim
                    filtered_claims.append(_apply_detail_level(_claim_to_dict(claim_obj), detail_level))
            else:
                filtered_claims = [
                    _apply_detail_level(_claim_to_dict(row["claim"]), detail_level)
                    for row in rows_data
                ]
            response["claims"] = filtered_claims
        return response

    @mcp.tool()
    def volunteer_context(
        query: str,
        db: str = "memorymaster.db",
        workspace: str = ".",
        token_budget: int = 4000,
        output_format: str = "text",
        limit: int = 100,
        retrieval_mode: str = "legacy",
        trust_mode: str = "trusted",
        include_stale: bool | None = None,
        include_conflicted: bool | None = None,
        include_candidates: bool | None = None,
        allow_sensitive: bool = False,
        scope_allowlist: str = "",
        detail_level: str = "standard",
        min_confidence: float = 0.65,
    ) -> dict[str, Any]:
        """Push/volunteer relevant context — confidence-gated, zero-LLM.

        Like ``query_for_context`` but adds a ``min_confidence`` gate so a
        pre-prompt hook can *proactively* surface only high-confidence claims
        from recent turns without flooding the window with weak guesses.

        The gate is a pure post-filter on the ranked rows: only claims whose
        ``confidence >= min_confidence`` survive before packing. The default
        (0.65, the gbrain-inspired volunteer threshold) is what distinguishes
        this tool from ``query_for_context`` — only claims worth volunteering
        unprompted survive. Pass ``min_confidence=0.0`` to open the gate fully,
        which makes the output identical to ``query_for_context`` with the same
        arguments. No LLM call is made; ranking, sensitivity filtering and
        scope handling are inherited unchanged from ``query_rows`` (sensitive
        claims are already dropped there, so none can be volunteered).

        detail_level options (applied to the structured claims list in the response):
          - "summary": claim_id, human_id, status, confidence, text[:80]
          - "standard" (default): full claim dict
          - "full": full claim dict with citations inlined
        """
        from memorymaster.recall.context_optimizer import pack_context

        limit = _bounded_limit(limit, maximum=250)
        token_budget = _bounded_limit(token_budget, maximum=32_000)

        resolve_allow_sensitive_access(
            allow_sensitive=allow_sensitive,
            context="mcp.volunteer_context",
        )
        svc = _read_service(db, workspace)
        effective_allowlist = _effective_scope_allowlist(scope_allowlist, workspace)
        from memorymaster.recall.planner import RetrievalRequest
        retrieval = svc.retrieve(RetrievalRequest(
            query_text=query,
            limit=limit,
            trust_mode=trust_mode,
            retrieval_mode=retrieval_mode,
            include_stale=include_stale,
            include_conflicted=include_conflicted,
            include_candidates=include_candidates,
            allow_sensitive=allow_sensitive,
            scope_allowlist=tuple(effective_allowlist),
        ))
        rows = list(retrieval.rows)
        # Confidence gate — pure post-filter on the already-ranked, already
        # sensitivity-filtered rows. >= so min_confidence=0.0 keeps everything
        # (additive, no recall change vs query_for_context when the gate is open).
        gated_rows = [
            row
            for row in rows
            if float(getattr(row["claim"], "confidence", 0.0) or 0.0) >= min_confidence
        ]
        result = pack_context(
            gated_rows,
            token_budget=token_budget,
            output_format=output_format,
        )
        response: dict[str, Any] = {
            "ok": True,
            "output": result.output,
            "claims_considered": result.claims_considered,
            "claims_included": result.claims_included,
            "tokens_used": result.tokens_used,
            "token_budget": result.token_budget,
            "format": result.format,
            "min_confidence": min_confidence,
        }
        if detail_level != "standard":
            if detail_level == "full":
                filtered_claims = []
                for row in gated_rows:
                    cid = getattr(row["claim"], "id", None)
                    claim_obj = row["claim"]
                    if cid is not None:
                        full_claim = svc.store.get_claim(int(cid), include_citations=True)
                        if full_claim is not None:
                            claim_obj = full_claim
                    filtered_claims.append(_apply_detail_level(_claim_to_dict(claim_obj), detail_level))
            else:
                filtered_claims = [
                    _apply_detail_level(_claim_to_dict(row["claim"]), detail_level)
                    for row in gated_rows
                ]
            response["claims"] = filtered_claims
        return response

    @mcp.tool()
    def query_for_task(
        task_description: str,
        project_scope: str = "",
        db: str = "memorymaster.db",
        workspace: str = ".",
        token_budget: int = 800,
        skip_qdrant: bool = True,
    ) -> dict[str, Any]:
        """Look-ahead L1 — task-aware briefing for an upcoming PRD task.

        Wraps query_for_context with a `<task_briefing>` XML envelope so the
        receiving model sees the task context as a structured briefing block
        rather than generic memory recall.

        Use this in pre-prompt hooks / recipes to auto-inject relevant memory
        when starting work on a specific task. Sanitizes the task description
        (drops stop-words like "extend"/"implement", removes dashes from
        identifiers, OR-joins remaining tokens) so FTS5 returns broader matches.

        Returns: {"ok": True, "briefing": "<xml>...</xml>", "elapsed_ms": int}.
        Returns empty briefing when no claims match.
        """
        import time as _time
        from memorymaster.recall.context_hook import query_for_task as _qft

        t0 = _time.perf_counter()
        # Use scope from arg, else env, else derived from workspace.
        # Parity fix (audit: read-scope-mismatch): when no explicit scope is
        # given, derive it via the SAME _project_scope helper that ingest uses
        # (canonicalize_slug + env/disambiguation handling). The previous raw
        # basename made the read scope diverge from the write scope for any
        # workspace dirname that needed canonicalization (e.g. "foo - Copy",
        # "Foo (2)", "whatsappbot-final"), so briefings silently missed claims.
        effective_scope = (project_scope or "").strip()
        if not effective_scope:
            effective_scope = _project_scope(workspace)

        try:
            briefing = _qft(
                task_description,
                effective_scope,
                db_path=_resolve_db(db),
                token_budget=token_budget,
                skip_qdrant=skip_qdrant,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300], "briefing": ""}

        return {
            "ok": True,
            "briefing": briefing or "",
            "scope": effective_scope,
            "elapsed_ms": int((_time.perf_counter() - t0) * 1000),
        }

    @mcp.tool()
    def read_active_tasks(
        project_root: str = ".",
    ) -> dict[str, Any]:
        """Read and parse <project_root>/vault/active_tasks.md (or active_tasks.md at root).

        Returns the active task pointer (first in_progress, falling back to first
        pending) and the full task list. Goose-side equivalent of legacy
        wezbridge/src/task-parser.cjs — used by recipes that need to know what
        the user is currently working on. Path resolution:
            1. <project_root>/vault/active_tasks.md
            2. <project_root>/active_tasks.md
            3. None → returns ok=False with reason

        Output shape:
            {
              "ok": True,
              "active_task": {"id": "T-001", "title": "...", "status": "in_progress",
                              "narrative": "...", "yaml_block": {...}}  | None,
              "all_tasks": [ ... same shape ... ],
              "source_file": "<resolved path>",
            }
        """
        import re as _re
        from pathlib import Path as _Path
        candidates = [
            _Path(project_root) / "vault" / "active_tasks.md",
            _Path(project_root) / "active_tasks.md",
        ]
        source = next((c for c in candidates if c.is_file()), None)
        if source is None:
            return {"ok": False, "reason": "no active_tasks.md found", "candidates": [str(c) for c in candidates]}

        try:
            text = source.read_text(encoding="utf-8")
        except Exception as exc:
            return {"ok": False, "reason": f"read failed: {exc}", "source_file": str(source)}

        # Parse: split on "## " headers, each section has a ```yaml block``` + narrative.
        header_re = _re.compile(r"^##\s+(?:Task:\s+)?(.+?)\s*$", _re.MULTILINE)
        positions = [(m.start(), m.end(), m.group(1).strip()) for m in header_re.finditer(text)]

        all_tasks: list[dict[str, Any]] = []
        for i, (start, end, title) in enumerate(positions):
            body_start = end
            body_end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
            body = text[body_start:body_end]

            yaml_block = {}
            ym = _re.search(r"```yaml\s*\n(.*?)\n```", body, _re.DOTALL)
            if ym:
                for line in ym.group(1).splitlines():
                    kv = _re.match(r"^([A-Za-z0-9_]+)\s*:\s*(.*)$", line.strip())
                    if kv:
                        yaml_block[kv.group(1)] = kv.group(2).strip().strip('"\'')

            # Narrative = body with yaml block stripped
            narrative = _re.sub(r"```yaml\s*\n.*?\n```", "", body, flags=_re.DOTALL).strip()

            # Try to extract a task id from the title (e.g. "T-001 · Title" or "Task: foo")
            id_match = _re.search(r"\b(T-?\d+)\b", title)
            task_id = id_match.group(1) if id_match else f"task-{i+1}"

            all_tasks.append({
                "id": task_id,
                "title": title,
                "status": yaml_block.get("status", "unknown"),
                "narrative": narrative,
                "yaml": yaml_block,
            })

        in_progress = next((t for t in all_tasks if t["status"] == "in_progress"), None)
        pending = next((t for t in all_tasks if t["status"] == "pending"), None)
        active = in_progress or pending

        return {
            "ok": True,
            "active_task": active,
            "all_tasks": all_tasks,
            "source_file": str(source),
        }

    @mcp.tool()
    def list_claims(
        db: str = "memorymaster.db",
        workspace: str = ".",
        status: str = "",
        limit: int = 50,
        include_archived: bool = False,
        allow_sensitive: bool = False,
        holder: str = "",
        cursor: str = "",
    ) -> dict[str, Any]:
        """List claims by optional status and/or belief holder (takes-vs-facts)."""
        resolve_allow_sensitive_access(
            allow_sensitive=allow_sensitive,
            context="mcp.list_claims",
        )
        svc = _read_service(db, workspace)
        limit = _bounded_limit(limit, maximum=250)
        claims, next_cursor = svc.list_claims_page(
            status=_empty_to_none(status),
            limit=limit,
            cursor=cursor,
            include_archived=include_archived,
            allow_sensitive=allow_sensitive,
            holder=_empty_to_none(holder),
            scope_allowlist=_effective_scope_allowlist("", workspace),
            requesting_agent=_team_request_principal(),
        )
        return {
            "ok": True,
            "rows": len(claims),
            "claims": [_claim_to_dict(c) for c in claims],
            "next_cursor": next_cursor or None,
        }

    @mcp.tool()
    def ingest_rule(
        trigger: str,
        action: str,
        rationale: str = "",
        db: str = "memorymaster.db",
        workspace: str = ".",
        scope: str = "project",
        source_agent: str = "mcp",
    ) -> dict[str, Any]:
        """Ingest a prescriptive rule-shaped claim (v3.21.0-R1).

        A rule captures "when <trigger>, do <action> because <rationale>" —
        the behavioural shape, distinct from descriptive fact claims. Stored
        as a claim_type='rule' claim; retrieve via query_rules.
        """
        from memorymaster.knowledge.rules import build_rule_fields

        try:
            fields = build_rule_fields(trigger, action, rationale)
        except ValueError as exc:
            return _structured_error(str(exc), "VALIDATION_ERROR", "trigger")

        # Parity with ingest_claim (audit: ingest_rule-skips-guards): a rule is
        # an ingest path, so it MUST share the same rate-limit guard and the
        # same sensitive-input rejection. Scan the rendered text + rationale +
        # action so a secret hidden in any rule field is caught before persist.
        rate_limit_error = _check_ingest_rate_limit(source_agent)
        if rate_limit_error is not None:
            return rate_limit_error
        for guard_field, guard_text in (
            ("text", fields["text"]),
            ("rationale", rationale),
            ("action", action),
        ):
            sensitive_error = _sensitive_input_error(guard_text, field=guard_field)
            if sensitive_error is not None:
                return sensitive_error

        svc = _service(db, workspace)
        # Auto-citation: a rule's provenance is the session that taught it.
        claim = svc.ingest(
            **fields,
            citations=[CitationInput(source=f"agent://{source_agent}", locator="rule", excerpt=action[:200])],
            scope=scope,
            source_agent=source_agent,
        )
        # Echo the SANITIZED persisted text (claim.text), never the raw
        # build_rule_fields output — returning fields["text"] would leak a
        # secret that the storage-time filter just firewalled at rest into the
        # client transcript / log.
        return {"ok": True, "claim_id": claim.id, "human_id": claim.human_id, "rule": claim.text}

    @mcp.tool()
    def query_rules(
        query: str,
        db: str = "memorymaster.db",
        workspace: str = ".",
        limit: int = 10,
        allow_sensitive: bool = False,
    ) -> dict[str, Any]:
        """Retrieve rule-shaped claims matching a query, in prescriptive form.

        Returns each rule's trigger / action / rationale / text, ranked by the
        hybrid retriever. Use this when you want only behavioural rules, not
        descriptive fact claims.
        """
        limit = _bounded_limit(limit, maximum=100)
        resolve_allow_sensitive_access(
            allow_sensitive=allow_sensitive,
            context="mcp.query_rules",
        )
        svc = _read_service(db, workspace)
        rules = svc.query_rules(query, limit=limit, allow_sensitive=allow_sensitive)
        return {"ok": True, "rows": len(rules), "rules": rules}

    @mcp.tool()
    def rules_export(
        db: str = "memorymaster.db",
        workspace: str = ".",
        min_confidence: float = 0.0,
        status: str = "",
        limit: int = 500,
        allow_sensitive: bool = False,
    ) -> dict[str, Any]:
        """Export mined rule-shaped claims, filtered by confidence + status.

        Read-only. Returns each rule's trigger / action / rationale / confidence
        / correction_count / status / created_at. Sensitive rules are filtered
        out unless ``allow_sensitive`` is granted by policy, so this never leaks
        another agent's secret rules.
        """
        limit = _bounded_limit(limit, maximum=1000)
        resolve_allow_sensitive_access(
            allow_sensitive=allow_sensitive,
            context="mcp.rules_export",
        )
        from memorymaster.knowledge.rule_export import collect_rules

        svc = _read_service(db, workspace)
        rows = collect_rules(
            svc,
            min_confidence=min_confidence,
            status=_empty_to_none(status),
            limit=limit,
            allow_sensitive=allow_sensitive,
        )
        return {"ok": True, "rows": len(rows), "rules": rows}

    @mcp.tool()
    def redact_claim_payload(
        claim_id: int,
        db: str = "memorymaster.db",
        workspace: str = ".",
        mode: str = "redact",
        redact_claim: bool = True,
        redact_citations: bool = True,
        reason: str = "",
        actor: str = "mcp",
    ) -> dict[str, Any]:
        """Redact or erase claim/citation payload non-destructively with audit event."""
        svc = _service(db, workspace)
        result = svc.redact_claim_payload(
            claim_id=claim_id,
            mode=mode,
            redact_claim=redact_claim,
            redact_citations=redact_citations,
            reason=_empty_to_none(reason),
            actor=actor,
        )
        payload = dict(result)
        claim_obj = payload.get("claim")
        if claim_obj is not None:
            payload["claim"] = _claim_to_dict(claim_obj)
        return {"ok": True, "result": payload}

    @mcp.tool()
    def pin_claim(
        claim_id: int,
        db: str = "memorymaster.db",
        workspace: str = ".",
        unpin: bool = False,
    ) -> dict[str, Any]:
        """Pin or unpin claim by id."""
        svc = _service(db, workspace)
        claim = svc.pin(claim_id=claim_id, pin=not unpin)
        return {"ok": True, "claim": _claim_to_dict(claim)}

    @mcp.tool()
    def compact_memory(
        db: str = "memorymaster.db",
        workspace: str = ".",
        retain_days: int = 30,
        event_retain_days: int = 60,
    ) -> dict[str, Any]:
        """Archive old stale/superseded/conflicted claims and trim events."""
        svc = _service(db, workspace)
        result = svc.compact(retain_days=retain_days, event_retain_days=event_retain_days)
        return {"ok": True, "result": result}

    @mcp.tool()
    def list_events(
        db: str = "memorymaster.db",
        workspace: str = ".",
        claim_id: int | None = None,
        event_type: str = "",
        limit: int = 100,
        cursor: str = "",
    ) -> dict[str, Any]:
        """List events by optional claim_id and event_type."""
        svc = _read_service(db, workspace)
        limit = _bounded_limit(limit, maximum=500)
        events, next_cursor = svc.list_events_page(
            claim_id=claim_id,
            limit=limit,
            event_type=_empty_to_none(event_type),
            cursor=cursor,
        )
        return {
            "ok": True,
            "rows": len(events),
            "events": [asdict(e) for e in events],
            "next_cursor": next_cursor or None,
        }

    @mcp.tool()
    def search_verbatim(
        query: str,
        db: str = "memorymaster.db",
        scope: str = "",
        limit: int = 10,
        mode: str = "fts",
    ) -> dict[str, Any]:
        """Search raw conversation memories (verbatim, unsummarized).

        ``vector`` and ``hybrid`` temporarily use authoritative FTS because
        direct Qdrant payload retrieval is quarantined pending rehydration.
        Use this when query_memory (claims) doesn't find what you need —
        verbatim search finds exact conversation fragments.
        """
        from memorymaster.recall.verbatim_store import search_verbatim as _search
        limit = _bounded_limit(limit, maximum=100)
        requested_mode = str(mode).strip().lower()
        effective_mode = "fts" if requested_mode in {"vector", "hybrid"} else requested_mode
        results = _search(
            _resolve_db(db), query, scope=scope or None, limit=limit, mode=effective_mode
        )
        response = {"ok": True, "rows": len(results), "results": results}
        if effective_mode != requested_mode:
            response.update({
                "requested_mode": requested_mode,
                "mode": effective_mode,
                "containment_reason": (
                    "verbatim qdrant retrieval is quarantined pending authoritative rehydration"
                ),
            })
        return response

    @mcp.tool()
    def get_usage_rollup(
        db: str = "memorymaster.db",
    ) -> dict[str, Any]:
        """Aggregate recall/ingest telemetry per source agent and session.

        Returns the Prometheus-style ``metrics_text`` (including the
        ``recalls_queried_total`` and ``claims_ingested_total`` families) plus
        the agent sessions active in the last hour. Exposes only aggregate
        counters and session metadata — never claim text.
        """
        return _usage_rollup(db)

    @mcp.tool()
    def open_dashboard(
        host: str = "127.0.0.1",
        port: int = 8765,
        path: str = "/dashboard",
        check_health: bool = True,
    ) -> dict[str, Any]:
        """
        Return the local dashboard URL and optionally check reachability.

        This tool does not start the dashboard process; start it with:
        `python -m memorymaster --db memorymaster.db run-dashboard`
        """
        clean_path = path if str(path).startswith("/") else f"/{path}"
        url = f"http://{host}:{int(port)}{clean_path}"
        reachable = None
        health_status = None
        health_payload: dict[str, Any] | None = None
        error = None

        if check_health:
            parsed = urlparse(f"http://{host}:{int(port)}/health")
            conn = None
            try:
                conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2.0)
                conn.request("GET", parsed.path)
                res = conn.getresponse()
                health_status = int(res.status)
                body = res.read().decode("utf-8", errors="replace").strip()
                if body:
                    try:
                        parsed_body = json.loads(body)
                        if isinstance(parsed_body, dict):
                            health_payload = parsed_body
                    except json.JSONDecodeError:
                        health_payload = None
                reachable = health_status == 200
            except Exception as exc:
                reachable = False
                error = str(exc)
            finally:
                if conn is not None:
                    conn.close()

        return {
            "ok": True,
            "url": url,
            "reachable": reachable,
            "health_status": health_status,
            "health_payload": health_payload,
            "error": error,
            "start_command": "python -m memorymaster --db memorymaster.db run-dashboard",
        }

    @mcp.tool()
    def list_steward_proposals(
        db: str = "memorymaster.db",
        workspace: str = ".",
        limit: int = 100,
        include_resolved: bool = False,
    ) -> dict[str, Any]:
        """List steward proposals for human override workflow."""
        from memorymaster.govern.steward import list_steward_proposals as _list_steward_proposals

        limit = _bounded_limit(limit, maximum=500)
        svc = _read_service(db, workspace)
        rows = _list_steward_proposals(
            svc,
            limit=limit,
            include_resolved=include_resolved,
        )
        return {"ok": True, "rows": len(rows), "proposals": rows}

    @mcp.tool()
    def resolve_steward_proposal(
        action: str,
        db: str = "memorymaster.db",
        workspace: str = ".",
        proposal_event_id: int | None = None,
        claim_id: int | None = None,
        apply_on_approve: bool = True,
    ) -> dict[str, Any]:
        """Approve or reject a steward proposal by proposal_event_id or claim_id."""
        from memorymaster.govern.steward import resolve_steward_proposal as _resolve_steward_proposal

        svc = _service(db, workspace)
        result = _resolve_steward_proposal(
            svc,
            action=str(action).strip().lower(),  # type: ignore[arg-type]
            proposal_event_id=proposal_event_id,
            claim_id=claim_id,
            apply_on_approve=apply_on_approve,
        )
        return {"ok": True, "result": result}

    @mcp.tool()
    def extract_entities(
        claim_id: int,
        text: str = "",
        db: str = "memorymaster.db",
        workspace: str = ".",
    ) -> dict[str, Any]:
        """Extract entities from a claim's text and link them to the knowledge graph."""
        from memorymaster.knowledge.entity_graph import EntityGraph, EntityGraphNotReady
        svc = _service(db, workspace)
        if not text:
            claim = svc.store.get_claim(claim_id, include_citations=False)
            if claim is None:
                return {"ok": False, "error": f"Claim {claim_id} not found"}
            text = claim.text
        eg = EntityGraph(_resolve_db(db))
        try:
            names = eg.extract_and_link(claim_id, text)
        except EntityGraphNotReady as exc:
            return _structured_error(str(exc), "ENTITY_GRAPH_NOT_READY")
        return {"ok": True, "entities": names, "count": len(names)}

    @mcp.tool()
    def entity_stats(
        db: str = "memorymaster.db",
        workspace: str = ".",
    ) -> dict[str, Any]:
        """Get entity knowledge graph statistics."""
        from memorymaster.knowledge.entity_graph import EntityGraph, EntityGraphNotReady

        _read_service(db, workspace)
        graph = EntityGraph(_resolve_db(db), read_only=True)
        try:
            return {"ok": True, **graph.get_stats()}
        except EntityGraphNotReady as exc:
            return _structured_error(str(exc), "ENTITY_GRAPH_NOT_READY")

    @mcp.tool()
    def find_related_claims(
        entity_names: str,
        db: str = "memorymaster.db",
        workspace: str = ".",
        hops: int = 2,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Find claims related to entities via knowledge graph traversal.

        entity_names: comma-separated entity names to search from.
        """
        from memorymaster.knowledge.entity_graph import EntityGraph, EntityGraphNotReady

        limit = _bounded_limit(limit, maximum=250)
        hops = _bounded_limit(hops, maximum=5)
        _read_service(db, workspace)
        graph = EntityGraph(_resolve_db(db), read_only=True)
        names = [n.strip() for n in entity_names.split(",") if n.strip()]
        try:
            claim_ids = graph.find_related_claims(names, hops=hops, limit=limit)
        except EntityGraphNotReady as exc:
            return _structured_error(str(exc), "ENTITY_GRAPH_NOT_READY")
        return {"ok": True, "claim_ids": claim_ids, "count": len(claim_ids)}

    @mcp.tool()
    def quality_scores(
        db: str = "memorymaster.db",
    ) -> dict[str, Any]:
        """Recompute quality scores for all claims based on usage feedback."""
        from memorymaster.govern.feedback import FeedbackTracker
        ft = FeedbackTracker(_resolve_db(db))
        ft.ensure_tables()
        result = ft.compute_quality_scores()
        stats = ft.get_stats()
        return {"ok": True, **result, **stats}

    @mcp.tool()
    def recompute_tiers(db: str = "memorymaster.db", workspace: str = ".") -> dict[str, Any]:
        """Recompute memory tiers (core/working/peripheral) based on access patterns."""
        svc = _service(db, workspace)
        result = svc.recompute_tiers()
        return {"ok": True, **result}

    @mcp.tool()
    def query_meta_decisions(
        query: str,
        claim_types: list[str] = ["decision", "architecture"],
        top_n: int = 20,
        db: str = "memorymaster.db",
        workspace: str = ".",
    ) -> dict[str, Any]:
        """Aggregate matching decision/architecture claims across all project scopes."""
        request = _validate_tool_input(
            QueryMetaDecisionsInput,
            {
                "query": query,
                "claim_types": claim_types,
                "top_n": top_n,
                "db": db,
                "workspace": workspace,
            },
        )
        if isinstance(request, dict):
            return request
        if request.top_n <= 0:
            return _structured_error("top_n must be positive.", "VALIDATION_ERROR", "top_n")

        svc = _read_service(request.db, request.workspace)
        result = svc.query_meta_decisions(
            query=request.query,
            claim_types=request.claim_types,
            top_n=request.top_n,
        )
        return {"ok": True, **result}

    @mcp.tool()
    def federated_query(
        query: str,
        db: str = "memorymaster.db",
        workspace: str = ".",
        limit: int = 20,
        current_scope: str = "",
        scope_allowlist: str = "",
    ) -> dict[str, Any]:
        """Query across ALL scopes — cross-project federation.

        Unlike query_memory which restricts results to the current project scope,
        this tool searches every claim regardless of scope, enabling cross-project
        memory retrieval. Returns claims sorted by relevance.
        """
        limit = _bounded_limit(limit, maximum=100)
        svc = _read_service(db, workspace)
        effective_scope = (current_scope or "").strip() or _project_scope(workspace)
        rows_data = svc.federated_query(
            query_text=query,
            limit=limit,
            current_scope=effective_scope,
            scope_allowlist=_parse_scope_allowlist(scope_allowlist),
        )
        claims = [row["claim"] for row in rows_data]
        serialized_claims = [_claim_to_dict(claim) for claim in claims]
        serialized_rows: list[dict[str, Any]] = [
            {
                "claim_index": index,
                "status": row.get("status"),
                "annotation": row.get("annotation", {}),
                "score": row.get("score", 0.0),
                "lexical_score": row.get("lexical_score", 0.0),
                "freshness_score": row.get("freshness_score", 0.0),
                "confidence_score": row.get("confidence_score", 0.0),
                "vector_score": row.get("vector_score", 0.0),
            }
            for index, row in enumerate(rows_data)
        ]
        return {
            "ok": True,
            "rows": len(claims),
            "claims": serialized_claims,
            "rows_data": serialized_rows,
            "response_contract": "memorymaster.retrieval.v2",
        }


def main() -> int:
    if FastMCP is None:  # pragma: no cover
        raise RuntimeError("MCP support is not installed. Install with: pip install 'memorymaster[mcp]'")
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
