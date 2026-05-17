import base64
import binascii
from dataclasses import asdict
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, TypeVar
import unicodedata
from urllib.parse import urlparse

from memorymaster import mcp_path_policy, observability
from pydantic import BaseModel, ValidationError

from memorymaster.models import CitationInput
from memorymaster.security import redact_text, resolve_allow_sensitive_access
from memorymaster.service import MemoryService

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
_INGEST_RATE_BUCKETS: dict[str, tuple[float, float]] = {}
_INGEST_RATE_BUCKETS_LOCK = threading.Lock()
_monotonic = time.monotonic
_SCOPE_SAFE_RE = re.compile(r"[^a-z0-9_-]+")
# Windows / macOS "Copy" artefacts and trailing "(1)"-style numeric variants.
# Applied to the workspace dirname BEFORE slug derivation so `foo - Copy - Copy`
# and `foo (2)` fold into `foo`.
_COPY_SUFFIX_RE = re.compile(
    r"(?:\s*[-_]?\s*copy(?:\s*[-_]?\s*copy)*|\s*\(\d+\)|_copy\d*)\s*$",
    re.IGNORECASE,
)
# Deployment-channel suffixes we fold away so `whatsappbot-final` and
# `whatsappbot-prod` both collapse to `whatsappbot`. Keep this list tight —
# adding words here changes scope identity for every workspace dirname that
# happens to end with one.
_CHANNEL_SUFFIX_RE = re.compile(r"-(?:final|prod|production|dev|staging|stage|qa|test)$")
_BASE64_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9+/=_-])"
    r"(?:[A-Za-z0-9+/]{20,}={0,2}|[A-Za-z0-9_-]{20,}={0,2})"
    r"(?![A-Za-z0-9+/=_-])"
)
_HEX_ESCAPE_SEQUENCE_RE = re.compile(r"(?:\\x[0-9A-Fa-f]{2}){4,}")
_CONFUSABLE_ASCII_MAP = str.maketrans({
    "\u0430": "a",  # Cyrillic small a
    "\u0410": "A",
    "\u0435": "e",
    "\u0415": "E",
    "\u043e": "o",
    "\u041e": "O",
    "\u0440": "p",
    "\u0420": "P",
    "\u0441": "c",
    "\u0421": "C",
    "\u0445": "x",
    "\u0425": "X",
    "\u0443": "y",
    "\u0423": "Y",
    "\u0456": "i",
    "\u0406": "I",
})
_MAX_SENSITIVITY_SCAN_VARIANTS = 64


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


def _add_sensitivity_variant(queue: list[str], seen: set[str], value: str) -> None:
    if not value or value in seen or len(seen) + len(queue) >= _MAX_SENSITIVITY_SCAN_VARIANTS:
        return
    seen.add(value)
    queue.append(value)


def _decode_text_bytes(raw: bytes) -> str | None:
    if not raw:
        return None
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\x00" in decoded:
        return None
    printable = sum(char.isprintable() or char in "\r\n\t" for char in decoded)
    return decoded if printable / max(len(decoded), 1) >= 0.85 else None


def _decode_base64_candidate(candidate: str) -> str | None:
    if len(candidate) % 4 == 1:
        return None
    padded = candidate + ("=" * (-len(candidate) % 4))
    try:
        return _decode_text_bytes(base64.b64decode(padded, validate=True))
    except binascii.Error:
        try:
            return _decode_text_bytes(base64.urlsafe_b64decode(padded))
        except (binascii.Error, ValueError):
            return None


def _decode_hex_escape_sequence(candidate: str) -> str | None:
    raw = bytes(int(pair, 16) for pair in re.findall(r"\\x([0-9A-Fa-f]{2})", candidate))
    return _decode_text_bytes(raw)


def _iter_json_scan_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_json_scan_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
                if isinstance(item, (str, int, float, bool)):
                    yield f"{key}={item}"
            yield from _iter_json_scan_strings(item)


def _extract_json_scan_strings(text: str):
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        yield from _iter_json_scan_strings(value)


def _sensitivity_scan_variants(text: str):
    seen = {text}
    queue = [text]
    while queue:
        current = queue.pop(0)
        yield current

        normalized = unicodedata.normalize("NFKC", current).translate(_CONFUSABLE_ASCII_MAP)
        _add_sensitivity_variant(queue, seen, normalized)

        for match in _HEX_ESCAPE_SEQUENCE_RE.finditer(current):
            decoded = _decode_hex_escape_sequence(match.group(0))
            if decoded:
                _add_sensitivity_variant(queue, seen, decoded)

        for match in _BASE64_CANDIDATE_RE.finditer(current):
            decoded = _decode_base64_candidate(match.group(0))
            if decoded:
                _add_sensitivity_variant(queue, seen, decoded)

        for nested in _extract_json_scan_strings(current):
            _add_sensitivity_variant(queue, seen, nested)


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


def _check_ingest_rate_limit(source_agent: str, now: float | None = None) -> dict[str, Any] | None:
    limit = _ingest_rate_limit_per_min()
    if limit == 0:
        return None

    timestamp = _monotonic() if now is None else now
    key = _empty_to_none(source_agent) or _ANONYMOUS_SOURCE_AGENT
    with _INGEST_RATE_BUCKETS_LOCK:
        tokens, last_refill = _INGEST_RATE_BUCKETS.get(key, (float(limit), timestamp))
        elapsed = max(0.0, timestamp - last_refill)
        refill_rate = limit / 60.0
        tokens = min(float(limit), tokens + elapsed * refill_rate)

        if tokens < 1.0:
            retry_after_ms = max(1, int(((1.0 - tokens) / refill_rate) * 1000))
            _INGEST_RATE_BUCKETS[key] = (tokens, timestamp)
            return _structured_error(
                f"ingest_claim rate limit exceeded for source_agent '{key}'.",
                "RATE_LIMITED",
                "source_agent",
                retry_after_ms=retry_after_ms,
                source_agent=key,
                limit_per_min=limit,
            )

        _INGEST_RATE_BUCKETS[key] = (tokens - 1.0, timestamp)
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


def _service(db: str, workspace: str) -> MemoryService:
    return MemoryService(db_target=_resolve_db(db), workspace_root=Path(_resolve_workspace(workspace)))


def _empty_to_none(value: str) -> str | None:
    v = value.strip()
    return v if v else None


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

    Rules (see ``omni/autoresearch-scope-canon-2026-04-22`` branch / audit):
      1. Lowercase + strip whitespace.
      2. Strip Windows/macOS ``- Copy``, ``- Copy - Copy``, ``(1)``, ``_copy``
         artefacts off the tail BEFORE slugifying (dirname-level).
      3. Slugify (non-alnum → ``-``) and trim leading/trailing ``_``/``-``/``.``
         so ``_omniclaude`` and ``omniclaude`` collide correctly.
      4. Fold deployment-channel suffixes (``-final``, ``-prod``, ``-dev``,
         ``-staging``, etc) so ``whatsappbot-final`` → ``whatsappbot``.
    """
    base = (dirname or "").strip().lower()
    prev = None
    while prev != base:
        prev = base
        base = _COPY_SUFFIX_RE.sub("", base).strip()
    if not base:
        return "workspace"
    slug = _SCOPE_SAFE_RE.sub("-", base).strip("-._") or "workspace"
    folded = _CHANNEL_SUFFIX_RE.sub("", slug)
    return folded or slug


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
    if not raw or raw == "project":
        return _project_scope(workspace)
    return raw


def _effective_scope_allowlist(raw: str, workspace: str) -> list[str] | None:
    parsed = _parse_scope_allowlist(raw)
    if parsed:
        return parsed
    scopes = [_project_scope(workspace), "global"]
    if _ENV_QUERY_INCLUDE_LEGACY_PROJECT:
        scopes.append("project")
    # Keep order and dedupe.
    seen: set[str] = set()
    deduped: list[str] = []
    for value in scopes:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _qdrant_query(query: str, db: str, workspace: str, limit: int) -> dict[str, Any]:
    """Fast semantic search via Qdrant+Ollama (no local model load)."""
    try:
        from memorymaster.qdrant_backend import QdrantBackend
    except ImportError:
        return {"ok": False, "error": "qdrant mode requires httpx. Install with: pip install 'memorymaster[qdrant]'"}
    backend = QdrantBackend()
    results = backend.search(query, limit=limit)
    backend.close()
    if not results:
        return {"ok": True, "rows": 0, "claims": [], "rows_data": []}

    # Enrich with full claim data from the DB
    svc = _service(db, workspace)
    enriched_rows: list[dict[str, Any]] = []
    enriched_claims: list[dict[str, Any]] = []
    for hit in results:
        cid = hit.get("claim_id")
        if cid is None:
            continue
        claim = svc.store.get_claim(int(cid), include_citations=True)
        if claim is None:
            # Claim may have been archived since last sync — return Qdrant payload
            enriched_rows.append({
                "claim": hit.get("payload", {}),
                "status": hit.get("payload", {}).get("state", "unknown"),
                "annotation": {},
                "score": hit.get("score", 0.0),
                "lexical_score": 0.0,
                "freshness_score": 0.0,
                "confidence_score": hit.get("payload", {}).get("confidence", 0.0),
                "vector_score": hit.get("score", 0.0),
            })
            enriched_claims.append(hit.get("payload", {}))
            continue
        claim_dict = _claim_to_dict(claim)
        enriched_claims.append(claim_dict)
        enriched_rows.append({
            "claim": claim_dict,
            "status": claim.status,
            "annotation": {
                "status": claim.status,
                "active": claim.status == "confirmed",
                "stale": claim.status == "stale",
                "conflicted": claim.status == "conflicted",
                "pinned": bool(claim.pinned),
            },
            "score": hit.get("score", 0.0),
            "lexical_score": 0.0,
            "freshness_score": 0.0,
            "confidence_score": claim.confidence,
            "vector_score": hit.get("score", 0.0),
        })
    return {
        "ok": True,
        "rows": len(enriched_claims),
        "claims": enriched_claims,
        "rows_data": enriched_rows,
        "retrieval_mode": "qdrant",
    }


if FastMCP is not None:
    mcp = FastMCP("memorymaster")

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
    ) -> dict[str, Any]:
        """
        Ingest a claim into memory.

        `sources_json` is a JSON array of `source|locator|excerpt` strings.
        `source_agent` identifies who created this claim (e.g. "claude-session", "codex-session").
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
            )
        except ValueError as exc:
            return _structured_error(str(exc), "VALIDATION_ERROR", "text")
        # Log to vault chronicle + cross-source synthesis
        try:
            from memorymaster.vault_log import log_ingest
            log_ingest(claim.id, claim.subject, claim.scope)
        except Exception as exc:
            logger.debug("Vault log failed: %s", exc)
        try:
            from memorymaster.vault_synthesis import synthesize_on_ingest
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
        from memorymaster.steward import run_steward as _run_steward

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
        """Classify a query and recommend the best retrieval mode."""
        from memorymaster.query_classifier import classify_query as _classify, recommended_retrieval_mode
        qtype = _classify(query)
        return {"query_type": qtype, "recommended_mode": recommended_retrieval_mode(qtype)}

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
        auto_classify: bool = False,
        include_stale: bool = True,
        include_conflicted: bool = True,
        include_candidates: bool = True,
        allow_sensitive: bool = False,
        scope_allowlist: str = "",
        detail_level: str = "standard",
    ) -> dict[str, Any]:
        """Query memory for relevant claims. Includes candidates by default for MCP use.

        retrieval_mode options:
          - "legacy" (default, fastest ~0.1s): SQL text search
          - "qdrant" (fast ~0.5s): semantic search via Qdrant+Ollama, requires QDRANT_URL
          - "hybrid" (slow ~8s): local sentence-transformers vector + lexical ranking

        auto_classify: when True and retrieval_mode is "legacy", classify the query
          automatically and upgrade to the recommended retrieval mode.

        detail_level options:
          - "summary": claim_id, human_id, status, confidence, text[:80]
          - "standard" (default): full claim dict
          - "full": full claim dict with citations inlined
        """
        from memorymaster.query_classifier import classify_query as _classify, recommended_retrieval_mode

        resolve_allow_sensitive_access(
            allow_sensitive=allow_sensitive,
            context="mcp.query_memory",
        )

        query_type: str | None = None
        if auto_classify and retrieval_mode == "legacy":
            query_type = _classify(query)
            retrieval_mode = recommended_retrieval_mode(query_type)

        # Qdrant retrieval mode: fast semantic search via network Qdrant+Ollama
        if retrieval_mode == "qdrant":
            result = _qdrant_query(query, db, workspace, limit)
            if query_type is not None:
                result["query_type"] = query_type
            if detail_level != "standard":
                result["claims"] = [_apply_detail_level(c, detail_level) for c in result.get("claims", [])]
            return result

        svc = _service(db, workspace)
        rows_data = svc.query_rows(
            query_text=query,
            limit=limit,
            retrieval_mode=retrieval_mode,
            include_stale=include_stale,
            include_conflicted=include_conflicted,
            include_candidates=include_candidates,
            allow_sensitive=allow_sensitive,
            scope_allowlist=_effective_scope_allowlist(scope_allowlist, workspace),
        )
        # For "full" detail level, re-fetch each claim with citations inline.
        if detail_level == "full":
            rows_data = _enrich_claims_with_citations(svc, rows_data)

        claims = [row["claim"] for row in rows_data]
        serialized_rows: list[dict[str, Any]] = []
        for row in rows_data:
            serialized_rows.append(
                {
                    "claim": _apply_detail_level(_claim_to_dict(row["claim"]), detail_level),
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
            "claims": [_apply_detail_level(_claim_to_dict(c), detail_level) for c in claims],
            "rows_data": serialized_rows,
        }
        if query_type is not None:
            response["query_type"] = query_type

        # Log to vault chronicle
        try:
            from memorymaster.vault_log import log_query
            log_query(query, len(claims))
        except Exception:
            pass

        return response

    @mcp.tool()
    def query_for_context(
        query: str,
        db: str = "memorymaster.db",
        workspace: str = ".",
        token_budget: int = 4000,
        output_format: str = "text",
        limit: int = 100,
        retrieval_mode: str = "legacy",
        include_stale: bool = True,
        include_conflicted: bool = True,
        include_candidates: bool = True,
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
        resolve_allow_sensitive_access(
            allow_sensitive=allow_sensitive,
            context="mcp.query_for_context",
        )
        svc = _service(db, workspace)
        result = svc.query_for_context(
            query=query,
            token_budget=token_budget,
            output_format=output_format,
            limit=limit,
            include_stale=include_stale,
            include_conflicted=include_conflicted,
            include_candidates=include_candidates,
            retrieval_mode=retrieval_mode,
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
            rows_data = svc.query_rows(
                query_text=query,
                limit=limit,
                retrieval_mode=retrieval_mode,
                include_stale=include_stale,
                include_conflicted=include_conflicted,
                include_candidates=include_candidates,
                allow_sensitive=allow_sensitive,
                scope_allowlist=_effective_scope_allowlist(scope_allowlist, workspace),
            )
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
        from memorymaster.context_hook import query_for_task as _qft

        t0 = _time.perf_counter()
        # Use scope from arg, else env, else derived from workspace.
        effective_scope = (project_scope or "").strip()
        if not effective_scope:
            from os.path import basename, normpath
            effective_scope = f"project:{basename(normpath(_resolve_workspace(workspace)))}"

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
    ) -> dict[str, Any]:
        """List claims by optional status."""
        resolve_allow_sensitive_access(
            allow_sensitive=allow_sensitive,
            context="mcp.list_claims",
        )
        svc = _service(db, workspace)
        claims = svc.list_claims(
            status=_empty_to_none(status),
            limit=limit,
            include_archived=include_archived,
            allow_sensitive=allow_sensitive,
        )
        return {"ok": True, "rows": len(claims), "claims": [_claim_to_dict(c) for c in claims]}

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
    ) -> dict[str, Any]:
        """List events by optional claim_id and event_type."""
        svc = _service(db, workspace)
        events = svc.list_events(
            claim_id=claim_id,
            limit=limit,
            event_type=_empty_to_none(event_type),
        )
        return {"ok": True, "rows": len(events), "events": [asdict(e) for e in events]}

    @mcp.tool()
    def search_verbatim(
        query: str,
        db: str = "memorymaster.db",
        scope: str = "",
        limit: int = 10,
        mode: str = "fts",
    ) -> dict[str, Any]:
        """Search raw conversation memories (verbatim, unsummarized).

        mode: "fts" (keyword), "vector" (Qdrant semantic), "hybrid" (both)
        Use this when query_memory (claims) doesn't find what you need —
        verbatim search finds exact conversation fragments.
        """
        from memorymaster.verbatim_store import search_verbatim as _search
        results = _search(_resolve_db(db), query, scope=scope or None, limit=limit, mode=mode)
        return {"ok": True, "rows": len(results), "results": results}

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
        from memorymaster.steward import list_steward_proposals as _list_steward_proposals

        svc = _service(db, workspace)
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
        from memorymaster.steward import resolve_steward_proposal as _resolve_steward_proposal

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
        from memorymaster.entity_graph import EntityGraph
        svc = _service(db, workspace)
        if not text:
            claim = svc.store.get_claim(claim_id, include_citations=False)
            if claim is None:
                return {"ok": False, "error": f"Claim {claim_id} not found"}
            text = claim.text
        eg = EntityGraph(_resolve_db(db))
        eg.ensure_tables()
        names = eg.extract_and_link(claim_id, text)
        return {"ok": True, "entities": names, "count": len(names)}

    @mcp.tool()
    def entity_stats(
        db: str = "memorymaster.db",
    ) -> dict[str, Any]:
        """Get entity knowledge graph statistics."""
        from memorymaster.entity_graph import EntityGraph
        eg = EntityGraph(_resolve_db(db))
        eg.ensure_tables()
        return {"ok": True, **eg.get_stats()}

    @mcp.tool()
    def find_related_claims(
        entity_names: str,
        db: str = "memorymaster.db",
        hops: int = 2,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Find claims related to entities via knowledge graph traversal.

        entity_names: comma-separated entity names to search from.
        """
        from memorymaster.entity_graph import EntityGraph
        eg = EntityGraph(_resolve_db(db))
        eg.ensure_tables()
        names = [n.strip() for n in entity_names.split(",") if n.strip()]
        claim_ids = eg.find_related_claims(names, hops=hops, limit=limit)
        return {"ok": True, "claim_ids": claim_ids, "count": len(claim_ids)}

    @mcp.tool()
    def quality_scores(
        db: str = "memorymaster.db",
    ) -> dict[str, Any]:
        """Recompute quality scores for all claims based on usage feedback."""
        from memorymaster.feedback import FeedbackTracker
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

        svc = _service(request.db, request.workspace)
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
        svc = _service(db, workspace)
        effective_scope = (current_scope or "").strip() or _project_scope(workspace)
        rows_data = svc.federated_query(
            query_text=query,
            limit=limit,
            current_scope=effective_scope,
            scope_allowlist=_parse_scope_allowlist(scope_allowlist),
        )
        claims = [row["claim"] for row in rows_data]
        serialized_rows: list[dict[str, Any]] = [
            {
                "claim": _claim_to_dict(row["claim"]),
                "status": row.get("status"),
                "annotation": row.get("annotation", {}),
                "score": row.get("score", 0.0),
                "lexical_score": row.get("lexical_score", 0.0),
                "freshness_score": row.get("freshness_score", 0.0),
                "confidence_score": row.get("confidence_score", 0.0),
                "vector_score": row.get("vector_score", 0.0),
            }
            for row in rows_data
        ]
        return {
            "ok": True,
            "rows": len(claims),
            "claims": [_claim_to_dict(c) for c in claims],
            "rows_data": serialized_rows,
        }


def main() -> int:
    if FastMCP is None:  # pragma: no cover
        raise RuntimeError("MCP support is not installed. Install with: pip install 'memorymaster[mcp]'")
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
