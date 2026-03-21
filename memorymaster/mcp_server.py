from dataclasses import asdict
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

from memorymaster.models import CitationInput
from memorymaster.security import resolve_allow_sensitive_access
from memorymaster.service import MemoryService

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover
    FastMCP = None  # type: ignore


_DEFAULT_DB = "memorymaster.db"
_DEFAULT_WORKSPACE = "."
_ENV_DEFAULT_DB = os.environ.get("MEMORYMASTER_DEFAULT_DB", "").strip()
_ENV_DEFAULT_WORKSPACE = os.environ.get("MEMORYMASTER_WORKSPACE", "").strip()
_ENV_DEFAULT_PROJECT_SCOPE = os.environ.get("MEMORYMASTER_DEFAULT_PROJECT_SCOPE", "").strip()
_ENV_QUERY_INCLUDE_LEGACY_PROJECT = (
    os.environ.get("MEMORYMASTER_QUERY_INCLUDE_LEGACY_PROJECT", "1").strip().lower() not in {"0", "false", "no"}
)
_SCOPE_SAFE_RE = re.compile(r"[^a-z0-9_-]+")


def _resolve_db(db: str) -> str:
    raw = str(db or "").strip()
    if raw and (raw != _DEFAULT_DB or not _ENV_DEFAULT_DB):
        return raw
    return _ENV_DEFAULT_DB or _DEFAULT_DB


def _resolve_workspace(workspace: str) -> str:
    raw = str(workspace or "").strip()
    if raw:
        return raw
    return _ENV_DEFAULT_WORKSPACE or _DEFAULT_WORKSPACE


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


def _project_scope(workspace: str) -> str:
    if _ENV_DEFAULT_PROJECT_SCOPE:
        return _ENV_DEFAULT_PROJECT_SCOPE
    workspace_path = Path(_resolve_workspace(workspace)).resolve()
    slug_base = workspace_path.name.strip().lower() or "workspace"
    slug = _SCOPE_SAFE_RE.sub("-", slug_base).strip("-") or "workspace"
    digest = hashlib.sha1(str(workspace_path).lower().encode("utf-8")).hexdigest()[:8]
    return f"project:{slug}:{digest}"


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
    ) -> dict[str, Any]:
        """
        Ingest a claim into memory.

        `sources_json` is a JSON array of `source|locator|excerpt` strings.
        Bi-temporal fields (ISO-8601 strings, all optional):
          - event_time: when the fact occurred in the real world
          - valid_from: start of the claim validity window
          - valid_until: end of the validity window (omit if still current)
        """
        svc = _service(db, workspace)
        claim = svc.ingest(
            text=text,
            citations=_parse_sources_json(sources_json),
            idempotency_key=_empty_to_none(idempotency_key),
            claim_type=_empty_to_none(claim_type),
            subject=_empty_to_none(subject),
            predicate=_empty_to_none(predicate),
            object_value=_empty_to_none(object_value),
            scope=_effective_ingest_scope(scope, workspace),
            volatility=volatility,
            confidence=confidence,
            event_time=_empty_to_none(event_time),
            valid_from=_empty_to_none(valid_from),
            valid_until=_empty_to_none(valid_until),
        )
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
    ) -> dict[str, Any]:
        """Query memory for relevant claims. Includes candidates by default for MCP use.

        retrieval_mode options:
          - "legacy" (default, fastest ~0.1s): SQL text search
          - "qdrant" (fast ~0.5s): semantic search via Qdrant+Ollama, requires QDRANT_URL
          - "hybrid" (slow ~8s): local sentence-transformers vector + lexical ranking

        auto_classify: when True and retrieval_mode is "legacy", classify the query
          automatically and upgrade to the recommended retrieval mode.
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
        claims = [row["claim"] for row in rows_data]
        serialized_rows: list[dict[str, Any]] = []
        for row in rows_data:
            serialized_rows.append(
                {
                    "claim": _claim_to_dict(row["claim"]),
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
            "claims": [_claim_to_dict(c) for c in claims],
            "rows_data": serialized_rows,
        }
        if query_type is not None:
            response["query_type"] = query_type
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
    ) -> dict[str, Any]:
        """Pack the most relevant claims into a token-budgeted context block.

        THE context window optimizer for AI agents. Returns a formatted text
        block (text, xml, or json) that fits within `token_budget` tokens,
        ranked by relevance using hybrid search (lexical + vector + freshness).

        Use this instead of query_memory when you need to inject memory
        directly into a system prompt or context window.
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
        return {
            "ok": True,
            "output": result.output,
            "claims_considered": result.claims_considered,
            "claims_included": result.claims_included,
            "tokens_used": result.tokens_used,
            "token_budget": result.token_budget,
            "format": result.format,
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


def main() -> int:
    if FastMCP is None:  # pragma: no cover
        raise RuntimeError("MCP support is not installed. Install with: pip install 'memorymaster[mcp]'")
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
