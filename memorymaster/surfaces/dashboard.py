from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
from html import escape
import json
import os
import subprocess
import sys
import time
from collections import Counter, deque
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

from memorymaster.surfaces import capture_inbox as capture_inbox_surface, cohort_review_template, dashboard_auth, dashboard_summary, dashboard_template, graph_observations_dashboard as graph_observations_surface, jev_dashboard, session_scope as session_scope_surface
from memorymaster.core.config import get_config
from memorymaster.govern.review import build_review_queue
from memorymaster.core.service import MemoryService
from memorymaster.recall.qdrant_transport import QdrantTransportConfig
from memorymaster.surfaces.dashboard_commands import (
    apply_triage_action,
    control_operator,
    update_action_proposal_status,
)
from memorymaster.surfaces.dashboard_read_models import (
    action_proposals_payload,
    audit_payload,
    claims_payload,
    conflicts_payload,
    events_payload,
    mobile_review_queue_payload,
    namespaces_payload,
    review_queue_payload,
    triage_flags,
)
import contextlib


def _first_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    value = str(values[-1]).strip()
    return value if value else None


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def _parse_int(value: str | None, *, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    parsed = int(value)
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"Expected integer in range [{minimum}, {maximum}], got {parsed}")
    return parsed


def _tail_events_from_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or not path.exists():
        return []
    rows: deque[dict[str, Any]] = deque(maxlen=limit)
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip().lstrip("\ufeff")
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
    return list(rows)


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    if quantile <= 0.0:
        return min(values)
    if quantile >= 1.0:
        return max(values)
    ordered = sorted(float(value) for value in values)
    index = (len(ordered) - 1) * quantile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return (ordered[lower] * (1.0 - weight)) + (ordered[upper] * weight)


def _latency_summary_from_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, float | int]]:
    by_metric: dict[str, list[float]] = {}
    for row in rows:
        for key, value in row.items():
            if key.endswith("_ms") and isinstance(value, (int, float)) and not isinstance(value, bool):
                by_metric.setdefault(key, []).append(float(value))
    return {
        name: {
            "count": len(values),
            "p50": (_percentile(values, 0.50) or 0.0),
            "p95": (_percentile(values, 0.95) or 0.0),
            "avg": (sum(values) / max(1, len(values))),
            "max": (max(values) if values else 0.0),
        }
        for name, values in sorted(by_metric.items())
    }


def _validation_latency_metric(service: Any) -> dict[str, Any]:
    store = getattr(service, "store", None)
    connect = getattr(store, "connect", None)
    if not callable(connect):
        return {"ok": True, "metric": "validation_latency", "unit": "seconds", "rows": 0, "p50": None, "p95": None, "p99": None}
    with connect() as conn:
        rows = conn.execute(
            """
            WITH first_validation AS (
                SELECT claim_id, MIN(created_at) AS validated_at
                FROM events
                WHERE claim_id IS NOT NULL
                  AND event_type IN ('validator', 'deterministic_validator')
                GROUP BY claim_id
            )
            SELECT
                c.id AS claim_id,
                c.created_at AS created_at,
                first_validation.validated_at AS validated_at,
                MAX(
                    0.0,
                    (julianday(first_validation.validated_at) - julianday(c.created_at)) * 86400.0
                ) AS latency_seconds
            FROM claims c
            JOIN first_validation ON first_validation.claim_id = c.id
            WHERE c.created_at IS NOT NULL
              AND first_validation.validated_at IS NOT NULL
            ORDER BY c.id
            """
        ).fetchall()
    samples = [
        {
            "claim_id": int(row["claim_id"]),
            "created_at": str(row["created_at"]),
            "validated_at": str(row["validated_at"]),
            "latency_seconds": float(row["latency_seconds"]),
        }
        for row in rows
        if row["latency_seconds"] is not None
    ]
    values = [sample["latency_seconds"] for sample in samples]
    return {
        "ok": True,
        "metric": "validation_latency",
        "unit": "seconds",
        "rows": len(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
    }


def _provenance_rows(service: Any) -> list[dict[str, Any]]:
    """One row per source_agent: total + status mix + last-ingest + 24h count.

    Read-only GROUP BY over claims (excludes archived). CASE-based SUMs are used
    instead of SQLite's `SUM(status='x')` boolean trick so the same query is valid
    on Postgres (storage-parity boundary — no schema change). NULL/empty
    source_agent collapses to the literal '<null>' bucket so it is visible (those
    are exactly the rows the Codex BEAT-3 script exists to convert into a clean
    'codex-session' total)."""
    store = getattr(service, "store", None)
    connect = getattr(store, "connect", None)
    if not callable(connect):
        return []
    sql = """
        SELECT
            COALESCE(NULLIF(TRIM(source_agent), ''), '<null>') AS agent,
            COUNT(*)                                            AS total,
            SUM(CASE WHEN status = 'confirmed'  THEN 1 ELSE 0 END) AS confirmed,
            SUM(CASE WHEN status = 'candidate'  THEN 1 ELSE 0 END) AS candidate,
            SUM(CASE WHEN status = 'stale'      THEN 1 ELSE 0 END) AS stale,
            SUM(CASE WHEN status = 'conflicted' THEN 1 ELSE 0 END) AS conflicted,
            MAX(created_at)                                    AS last_ingest,
            SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END)   AS ingests_24h
        FROM claims
        WHERE status != 'archived'
        GROUP BY agent
        ORDER BY total DESC
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    with connect() as conn:
        rows = conn.execute(sql, (cutoff,)).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "agent": str(row["agent"]),
                "total": int(row["total"] or 0),
                "confirmed": int(row["confirmed"] or 0),
                "candidate": int(row["candidate"] or 0),
                "stale": int(row["stale"] or 0),
                "conflicted": int(row["conflicted"] or 0),
                "last_ingest": (str(row["last_ingest"]) if row["last_ingest"] else None),
                "ingests_24h": int(row["ingests_24h"] or 0),
            }
        )
    return out


def _ingest_recall_note() -> dict[str, Any]:
    """Honesty marker for the provenance panel.

    Ingest IS attributed per source_agent (claims.source_agent, reliable post-P3
    intake policy). Recall is NOT attributed at the row level: the events table
    has no source_agent column, so a per-agent recall split would be fabricated.
    The panel surfaces this flag so the UI can say so explicitly rather than
    invent numbers."""
    return {"ingest_attributed": True, "recall_attributed": False}


def _claim_to_dict(claim: Any) -> dict[str, Any]:
    return asdict(claim)


def _parse_review_queue_limit(value: str | None) -> int:
    if value is None:
        return 5
    return max(1, min(int(value), 100))


def _parse_review_queue_cursor(value: str | None) -> int | None:
    if value is None:
        return None
    cursor = int(value)
    if cursor <= 0:
        raise ValueError("cursor must be positive")
    return cursor


def _claim_age_days(created_at: str, now: datetime) -> float:
    normalized = str(created_at).strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    created = datetime.fromisoformat(normalized)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0.0, (now - created.astimezone(timezone.utc)).total_seconds() / 86400.0)


def _event_to_dict(event: Any) -> dict[str, Any]:
    payload = asdict(event)
    raw = payload.get("payload_json")
    parsed = None
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
    payload["payload"] = parsed
    return payload


def _build_retrieval_method_attempts(
    limit: int,
    include_stale: bool,
    include_conflicted: bool,
    mode: str,
    allow_sensitive: bool,
    scope_allowlist: list[str],
    with_scope: bool,
) -> list[dict[str, Any]]:
    """Build fallback attempts for retrieval method kwargs."""
    kwargs: dict[str, Any] = {
        "limit": limit,
        "include_stale": include_stale,
        "include_conflicted": include_conflicted,
        "retrieval_mode": mode,
        "allow_sensitive": allow_sensitive,
    }
    if with_scope and scope_allowlist:
        kwargs["scope_allowlist"] = scope_allowlist
    attempts: list[dict[str, Any]] = [kwargs]
    if "scope_allowlist" in kwargs:
        fallback = dict(kwargs)
        fallback.pop("scope_allowlist", None)
        attempts.append(fallback)
    if "allow_sensitive" in kwargs:
        fallback = dict(attempts[-1])
        fallback.pop("allow_sensitive", None)
        attempts.append(fallback)
    return attempts


def _build_get_route_map(handler: Any) -> dict[str, callable]:
    """Build a mapping of routes to handler callables."""
    return {
        "/health": lambda qs: handler._write_json({"ok": True, "service": "memorymaster-dashboard"}),
        "/healthz": lambda qs: handler._handle_healthz(),
        "/readyz": lambda qs: handler._handle_readyz(),
        "/": lambda qs: handler._write_dashboard(),
        "/dashboard": lambda qs: handler._write_dashboard(),
        "/review": lambda qs: handler._write_html(cohort_review_template.REVIEW_HTML.replace("__REVIEW_PACKET_JSON__", "null")),
        "/decisions": lambda qs: jev_dashboard.write_decisions_page(handler, escape(_package_version())),
        "/api/decisions/metrics": lambda qs: jev_dashboard.write_metrics_response(handler, qs),
        "/api/decisions/review-queue": lambda qs: jev_dashboard.write_review_queue_response(handler, qs),
        "/favicon.ico": lambda qs: capture_inbox_surface.write_favicon_response(handler, qs),
        "/api/decision-summary": lambda qs: dashboard_summary.write_summary_response(handler, qs),
        "/api/claims": lambda qs: handler._handle_claims(qs),
        "/api/capture-inbox": lambda qs: capture_inbox_surface.write_capture_inbox_response(handler, qs),
        "/api/graph-observations": lambda qs: graph_observations_surface.write_graph_observations_response(handler, qs),
        "/api/events": lambda qs: handler._handle_events(qs),
        "/api/timeline": lambda qs: handler._handle_timeline(qs),
        "/api/conflicts": lambda qs: handler._handle_conflicts(qs),
        "/api/review-queue": lambda qs: handler._handle_review_queue(qs),
        "/api/v1/review-queue": lambda qs: handler._handle_mobile_review_queue(qs),
        "/api/action-proposals": lambda qs: handler._handle_action_proposals(qs),
        "/api/atlas/version": lambda qs: handler._handle_atlas_version(qs),
        "/api/retrieval": lambda qs: handler._handle_retrieval(qs),
        "/api/recall-analysis": lambda qs: handler._handle_recall_analysis(qs),
        "/api/audit": lambda qs: handler._handle_audit(qs),
        "/api/namespaces": lambda qs: handler._handle_namespaces(qs),
        "/api/provenance": lambda qs: handler._handle_provenance(qs),
        "/api/session-stats": lambda qs: handler._handle_session_stats(qs),
        "/api/session-bindings": lambda qs: session_scope_surface.write_session_scope_response(handler, qs),
        "/api/observability": lambda qs: handler._handle_observability(qs),
        "/api/integrity": lambda qs: handler._handle_integrity(qs),
        "/metrics/validation-latency": lambda qs: handler._handle_validation_latency(qs),
        "/api/operator/status": lambda qs: handler._write_json({"ok": True, **handler._server.operator_status()}),
        "/api/operator/stream": lambda qs: handler._handle_operator_stream(qs),
    }


def _route_get_request(handler: Any, route: str, query_string: str) -> bool:
    """Route a GET request to the appropriate handler. Returns True if routed."""
    claim_id = _parse_claim_lineage_route(route)
    if claim_id is not None:
        handler._handle_claim_lineage(claim_id)
        return True
    route_map = _build_get_route_map(handler)
    if route not in route_map:
        return False
    route_map[route](query_string)
    return True


def _parse_claim_lineage_route(route: str) -> int | None:
    parts = [part for part in route.split("/") if part]
    if len(parts) != 3 or parts[0] != "claim" or parts[2] != "lineage":
        return None
    claim_id = int(parts[1])
    if claim_id <= 0:
        raise ValueError("claim id must be positive")
    return claim_id


def _get_lineage_claim(service: Any, claim_id: int) -> Any | None:
    store = getattr(service, "store", None)
    get_claim = getattr(store, "get_claim", None)
    if callable(get_claim):
        return get_claim(claim_id, include_citations=False)
    for claim in service.list_claims(limit=5000, include_archived=True):
        if int(claim.id) == claim_id:
            return claim
    return None


def _lineage_candidate_claims(service: Any) -> list[Any]:
    store = getattr(service, "store", None)
    list_claims = getattr(store, "list_claims", None) or getattr(service, "list_claims", None)
    if not callable(list_claims):
        return []
    try:
        return list_claims(limit=5000, include_archived=True, include_citations=False)
    except TypeError:
        return list_claims(limit=5000, include_archived=True)


def _collect_reverse_lineage(claim_id: int, by_replacement: dict[int, list[Any]], seen: set[int]) -> list[Any]:
    rows: list[Any] = []
    for claim in sorted(by_replacement.get(claim_id, []), key=lambda item: int(item.id)):
        cid = int(claim.id)
        if cid in seen:
            continue
        seen.add(cid)
        rows.extend(_collect_reverse_lineage(cid, by_replacement, seen))
        rows.append(claim)
    return rows


def _build_claim_lineage(service: Any, target: Any) -> tuple[list[Any], list[tuple[int, int]]]:
    all_claims = _lineage_candidate_claims(service)
    by_replacement: dict[int, list[Any]] = {}
    for claim in all_claims:
        replacement_id = getattr(claim, "replaced_by_claim_id", None)
        if replacement_id is not None:
            by_replacement.setdefault(int(replacement_id), []).append(claim)

    target_id = int(target.id)
    seen = {target_id}
    predecessors = _collect_reverse_lineage(target_id, by_replacement, seen)
    successors: list[Any] = []
    current = target
    for _ in range(50):
        next_id = getattr(current, "replaced_by_claim_id", None)
        if next_id is None or int(next_id) in seen:
            break
        next_claim = _get_lineage_claim(service, int(next_id))
        if next_claim is None:
            break
        seen.add(int(next_claim.id))
        successors.append(next_claim)
        current = next_claim

    nodes = predecessors + [target] + successors
    node_ids = {int(claim.id) for claim in nodes}
    edges = [
        (int(claim.id), int(claim.replaced_by_claim_id))
        for claim in nodes
        if getattr(claim, "replaced_by_claim_id", None) in node_ids
    ]
    return nodes, edges


def _claim_lineage_title(claim: Any) -> str:
    bits = [getattr(claim, "subject", None), getattr(claim, "predicate", None), getattr(claim, "object_value", None)]
    title = " / ".join(str(bit) for bit in bits if bit not in (None, ""))
    return title or str(getattr(claim, "text", ""))


def _render_claim_lineage_svg(nodes: list[Any], edges: list[tuple[int, int]], target_id: int) -> str:
    node_width = 220
    node_gap = 70
    width = max(560, 40 + len(nodes) * node_width + max(0, len(nodes) - 1) * node_gap)
    height = 210
    positions = {int(claim.id): 24 + index * (node_width + node_gap) for index, claim in enumerate(nodes)}
    edge_markup = []
    for source_id, target in edges:
        x1 = positions[source_id] + node_width
        x2 = positions[target]
        edge_markup.append(
            f'<line x1="{x1}" y1="92" x2="{x2}" y2="92" stroke="#64748b" stroke-width="2" marker-end="url(#arrow)" />'
        )
    node_markup = []
    for claim in nodes:
        cid = int(claim.id)
        x = positions[cid]
        stroke = "#38bdf8" if cid == target_id else "#475569"
        fill = "#0f172a" if cid == target_id else "#1e293b"
        title = escape(_claim_lineage_title(claim)[:62])
        status = escape(str(getattr(claim, "status", "unknown")))
        replaced_by = getattr(claim, "replaced_by_claim_id", None)
        node_markup.append(
            f'<g class="claim-node{" target" if cid == target_id else ""}">'
            f'<rect x="{x}" y="38" width="{node_width}" height="108" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="2" />'
            f'<text x="{x + 14}" y="65" fill="#f8fafc" font-size="15" font-weight="700">#{cid}</text>'
            f'<text x="{x + 14}" y="89" fill="#cbd5e1" font-size="13">{status}</text>'
            f'<text x="{x + 14}" y="113" fill="#94a3b8" font-size="12">{title}</text>'
            f'<text x="{x + 14}" y="134" fill="#64748b" font-size="11">replaced_by_claim_id: {escape(str(replaced_by or "-"))}</text>'
            "</g>"
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Claim lineage graph">'
        '<defs><marker id="arrow" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">'
        '<path d="M0,0 L10,4 L0,8 Z" fill="#64748b" /></marker></defs>'
        f"{''.join(edge_markup)}{''.join(node_markup)}</svg>"
    )


def _render_claim_lineage_html(target: Any, nodes: list[Any], edges: list[tuple[int, int]]) -> str:
    target_id = int(target.id)
    svg = _render_claim_lineage_svg(nodes, edges, target_id)
    rows = []
    for claim in nodes:
        cid = int(claim.id)
        rows.append(
            "<tr>"
            f'<td class="mono">#{cid}</td>'
            f"<td>{escape(str(getattr(claim, 'status', 'unknown')))}</td>"
            f"<td>{escape(_claim_lineage_title(claim))}</td>"
            f"<td class=\"mono\">{escape(str(getattr(claim, 'replaced_by_claim_id', None) or '-'))}</td>"
            f"<td>{'target' if cid == target_id else ''}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claim Lineage #{target_id}</title>
<style>
body{{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;margin:0;background:#0f172a;color:#e2e8f0}}
main{{max-width:1200px;margin:0 auto;padding:24px}}
a{{color:#38bdf8}} h1{{margin:0 0 6px;color:#f8fafc;font-size:1.5rem}}
.muted{{color:#94a3b8}} .panel{{border:1px solid #334155;border-radius:10px;background:#1e293b;margin-top:18px;padding:16px;overflow:auto}}
.mono{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}} svg{{min-width:560px;width:100%;height:auto}}
table{{width:100%;border-collapse:collapse;margin-top:14px;font-size:.9rem}} th,td{{border-bottom:1px solid #334155;padding:8px;text-align:left}}
th{{color:#94a3b8;font-size:.75rem;text-transform:uppercase;letter-spacing:.05em}}
</style></head><body><main>
<a href="/dashboard">&larr; Dashboard</a>
<h1>Claim Lineage #{target_id}</h1>
<div class="muted">Forward edges follow replaced_by_claim_id; reverse lookup finds older claims replaced by this chain.</div>
<div class="panel">{svg}</div>
<div class="panel"><table><thead><tr><th>ID</th><th>Status</th><th>Claim</th><th>Replaced By</th><th>Marker</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
</main></body></html>"""


def _call_retrieval_method_with_fallback(method: Any, text: str, attempts: list[dict[str, Any]]) -> Any:
    """Call retrieval method with fallback attempts."""
    last_error: TypeError | None = None
    for attempt in attempts:
        try:
            return method(text, **attempt)
        except TypeError as exc:
            last_error = exc
            if "unexpected keyword argument" not in str(exc):
                raise
    if last_error is not None:
        raise last_error
    return []


def _parse_retrieval_query_params(query_string: str) -> dict[str, Any]:
    """Parse and validate query parameters for retrieval."""
    query = parse_qs(query_string)
    text = _first_query_value(query, "query") or ""
    mode = (_first_query_value(query, "mode") or "hybrid").strip().lower()
    mode = mode if mode in {"legacy", "hybrid"} else "hybrid"
    limit = _parse_int(_first_query_value(query, "limit"), default=10, minimum=1, maximum=100)
    include_stale = _parse_bool(_first_query_value(query, "include_stale"), default=False)
    include_conflicted = _parse_bool(_first_query_value(query, "include_conflicted"), default=False)
    allow_sensitive = _parse_bool(_first_query_value(query, "allow_sensitive"), default=False)

    scope_raw = _first_query_value(query, "scope_allowlist")
    scope_allowlist: list[str] = []
    if scope_raw:
        dedupe: set[str] = set()
        for token in scope_raw.split(","):
            normalized = token.strip()
            if normalized and normalized not in dedupe:
                dedupe.add(normalized)
                scope_allowlist.append(normalized)

    return {
        "text": text,
        "mode": mode,
        "limit": limit,
        "include_stale": include_stale,
        "include_conflicted": include_conflicted,
        "allow_sensitive": allow_sensitive,
        "scope_allowlist": scope_allowlist,
    }


def _extract_claims_from_rows(rows_data: Any) -> tuple[list[Any], dict[int, dict[str, Any]]]:
    """Extract claims and scores from query_rows result."""
    claims: list[Any] = []
    scored_by_claim_id: dict[int, dict[str, Any]] = {}

    if not isinstance(rows_data, list):
        return claims, scored_by_claim_id

    for row in rows_data:
        claim_obj = row.get("claim") if isinstance(row, dict) else getattr(row, "claim", None)
        if claim_obj is None:
            continue
        try:
            claim_id = int(claim_obj.id)
        except Exception:
            continue
        claims.append(claim_obj)
        if isinstance(row, dict):
            scored_by_claim_id[claim_id] = row
        else:
            scored_by_claim_id[claim_id] = {
                "score": getattr(row, "score", None),
                "lexical_score": getattr(row, "lexical_score", None),
                "confidence_score": getattr(row, "confidence_score", None),
                "freshness_score": getattr(row, "freshness_score", None),
                "vector_score": getattr(row, "vector_score", None),
            }

    return claims, scored_by_claim_id


def _package_version() -> str:
    from memorymaster import __version__

    return __version__


def _check_dashboard_db(service: Any) -> dict[str, Any]:
    store = getattr(service, "store", None)
    connect = getattr(store, "connect", None)
    if not callable(connect):
        return {"status": "fail", "error": "store connection is unavailable"}
    try:
        with connect() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        return {"status": "fail", "error": str(exc)}
    return {"status": "ok"}


def _qdrant_request_headers() -> dict[str, str]:
    return QdrantTransportConfig.from_env().headers()


def _check_qdrant(qdrant_url: str | None) -> dict[str, Any]:
    if not qdrant_url:
        return {"status": "skipped", "reason": "QDRANT_URL not set"}
    try:
        transport = QdrantTransportConfig.from_env()
        transport.validate_url(qdrant_url)
    except (OSError, RuntimeError, ValueError):
        return {"status": "fail", "error": "invalid Qdrant transport configuration"}
    base_url = qdrant_url.rstrip("/")
    last_error = ""
    for path in ("/healthz", "/collections"):
        request = transport.request(f"{base_url}{path}", method="GET")
        try:
            with transport.open(request, timeout=0.5) as response:
                if 200 <= int(response.status) < 300:
                    return {"status": "ok", "endpoint": path}
                last_error = f"HTTP {response.status} from {path}"
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code} from {path}"
        except Exception:
            last_error = f"Qdrant request failed for {path}"
    return {"status": "fail", "error": last_error or "Qdrant probe failed"}


def _compute_claim_row(claim: Any, query_tokens: set[str], scored_by_claim_id: dict[int, dict[str, Any]], triage_flags: dict[int, dict[str, bool]]) -> dict[str, Any]:
    """Compute a single claim row with scores and annotations."""
    claim_id = int(claim.id)
    c_tokens = set(str(claim.text or "").lower().split())
    lexical = (len(query_tokens & c_tokens) / max(1, len(query_tokens))) if query_tokens else 0.0
    confidence = max(0.0, min(1.0, float(claim.confidence)))
    freshness = 0.5
    vector = 0.0
    _w_l, _w_c, _w_f = get_config().retrieval_weights_no_vector
    score = (_w_l * lexical) + (_w_c * confidence) + (_w_f * freshness)

    scored = scored_by_claim_id.get(claim_id)
    if isinstance(scored, dict):
        with contextlib.suppress(TypeError, ValueError):
            lexical = float(scored.get("lexical_score", scored.get("lexical", lexical)))
        with contextlib.suppress(TypeError, ValueError):
            confidence = float(scored.get("confidence_score", scored.get("confidence", confidence)))
        with contextlib.suppress(TypeError, ValueError):
            freshness = float(scored.get("freshness_score", scored.get("freshness", freshness)))
        with contextlib.suppress(TypeError, ValueError):
            vector = float(scored.get("vector_score", scored.get("vector", vector)))
        with contextlib.suppress(TypeError, ValueError):
            score = float(scored.get("score", score))

    triage = triage_flags.get(claim_id, {"reviewed": False, "suppressed": False})
    annotation_parts: list[str] = []
    normalized_status = str(claim.status or "").strip().lower()
    if normalized_status == "stale":
        annotation_parts.append("stale: refresh or re-validate")
    if normalized_status == "conflicted":
        annotation_parts.append("conflicted: compare competing values")
    if bool(triage["reviewed"]):
        annotation_parts.append("triage reviewed")
    if bool(triage["suppressed"]):
        annotation_parts.append("triage suppressed")
    if bool(getattr(claim, "pinned", False)):
        annotation_parts.append("pinned")

    return {
        "claim": _claim_to_dict(claim),
        "status": str(claim.status or ""),
        "annotation": ", ".join(annotation_parts) if annotation_parts else "active",
        "triage_reviewed": bool(triage["reviewed"]),
        "triage_suppressed": bool(triage["suppressed"]),
        "score": float(score),
        "lexical_score": float(lexical),
        "confidence_score": float(confidence),
        "freshness_score": float(freshness),
        "vector_score": float(vector),
    }


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        service: MemoryService,
        operator_log_jsonl: str | Path,
        db_target: str | Path | None,
        workspace_root: str | Path | None,
    ) -> None:
        self.service = service
        self.operator_log_jsonl = Path(operator_log_jsonl)
        self.db_target = str(db_target) if db_target is not None else "memorymaster.db"
        self.workspace_root = Path(workspace_root) if workspace_root is not None else Path.cwd()
        self._operator_proc: subprocess.Popen[str] | None = None
        super().__init__(server_address, DashboardRequestHandler)
        host, port = server_address[0], self.server_address[1]
        self.configured_host_port = f"{'[' + host + ']' if ':' in host else host}:{port}"
        try:
            self.allowed_origins = dashboard_auth.allowed_origins(host, port)
        except ValueError:
            self.server_close()
            raise

    def operator_status(self) -> dict[str, Any]:
        proc = self._operator_proc
        running = proc is not None and proc.poll() is None
        return {
            "running": running,
            "pid": (proc.pid if running and proc is not None else None),
            "log_jsonl": str(self.operator_log_jsonl),
        }

    def start_operator(self, inbox_jsonl: str) -> dict[str, Any]:
        if self.operator_status()["running"]:
            return {"started": False, "reason": "already_running", **self.operator_status()}
        inbox = Path(inbox_jsonl)
        inbox.parent.mkdir(parents=True, exist_ok=True)
        inbox.touch(exist_ok=True)
        self.operator_log_jsonl.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            "-m",
            "memorymaster",
            "--db",
            self.db_target,
            "--workspace",
            str(self.workspace_root),
            "run-operator",
            "--inbox-jsonl",
            str(inbox),
            "--max-idle-seconds",
            "120",
            "--retrieval-mode",
            "hybrid",
            "--policy-mode",
            "cadence",
            "--log-jsonl",
            str(self.operator_log_jsonl),
        ]
        self._operator_proc = subprocess.Popen(  # noqa: S603
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(self.workspace_root),
            text=True,
        )
        return {"started": True, **self.operator_status()}

    def stop_operator(self) -> dict[str, Any]:
        proc = self._operator_proc
        if proc is None or proc.poll() is not None:
            self._operator_proc = None
            return {"stopped": False, "reason": "not_running", **self.operator_status()}
        try:
            proc.terminate()
            proc.wait(timeout=4)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
        self._operator_proc = None
        return {"stopped": True, **self.operator_status()}


class DashboardRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    @property
    def _server(self) -> DashboardHTTPServer:
        return self.server  # type: ignore[return-value]

    def _enforce_auth(self, *, method: str, route: str) -> bool:
        """Run the v3.19.0-H2 auth + role + CSRF gates. Returns True on pass.

        On failure, the JSON error response has already been written; the
        caller must return immediately without dispatching the route.
        Health endpoints (/health, /healthz, /readyz) are intentionally
        exempt so external monitors can probe without credentials.
        """
        host_check = dashboard_auth.check_host(self.headers, origins=self._server.allowed_origins)
        if not host_check.ok:
            self._write_json({"ok": False, "error": host_check.reason}, status=host_check.status)
            return False
        if route in {"/health", "/healthz", "/readyz"}:
            return True

        decision = dashboard_auth.authenticate(self.headers)
        decision = dashboard_auth.authorize(decision, method=method, route=route)
        if not decision.ok:
            self._write_json(
                {"ok": False, "error": decision.reason},
                status=decision.status,
            )
            return False

        csrf = dashboard_auth.check_csrf(
            self.headers, configured_host_port=self._server.configured_host_port,
            origins=self._server.allowed_origins,
        )
        if not csrf.ok:
            self._write_json({"ok": False, "error": csrf.reason}, status=csrf.status)
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            if not self._enforce_auth(method="GET", route=route):
                return
            if _route_get_request(self, route, parsed.query):
                return
            self._write_json({"ok": False, "error": "Not found"}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._write_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._write_json({"ok": False, "error": f"Internal server error: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            if not self._enforce_auth(method="POST", route=route):
                return
            payload = self._read_json_body()
            if route == "/api/triage/action":
                self._handle_triage_action(payload)
                return
            if route == "/api/operator/control":
                self._handle_operator_control(payload)
                return
            if route == "/api/action-proposals/status":
                self._handle_action_proposal_status(payload)
                return
            if route == "/api/capture-inbox/retire":
                capture_inbox_surface.write_capture_retirement_response(self, payload)
                return
            if route == "/api/decisions/review":
                jev_dashboard.write_review_label_response(self, payload)
                return
            self._write_json({"ok": False, "error": "Not found"}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._write_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._write_json({"ok": False, "error": f"Internal server error: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        data = self.rfile.read(length)
        parsed = json.loads(data.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("Expected JSON object body")
        return parsed

    def _write_json(self, payload: dict[str, Any], *, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_html(self, html: str, *, status: int = HTTPStatus.OK) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_healthz(self) -> None:
        payload: dict[str, Any] = {"status": "ok"}
        version = _package_version()
        if version is not None:
            payload["version"] = version
        self._write_json(payload)

    def _handle_readyz(self) -> None:
        checks = {
            "db": _check_dashboard_db(self._server.service),
            "qdrant": _check_qdrant(os.environ.get("QDRANT_URL")),
        }
        ready = all(check.get("status") in {"ok", "skipped"} for check in checks.values())
        status = HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE
        self._write_json(
            {"status": "ready" if ready else "not_ready", "checks": checks},
            status=status,
        )

    def _handle_claim_lineage(self, claim_id: int) -> None:
        target = _get_lineage_claim(self._server.service, claim_id)
        if target is None:
            self._write_html(
                "<!doctype html><html><head><title>Claim not found</title></head>"
                f"<body><h1>Claim #{claim_id} not found</h1></body></html>",
                status=HTTPStatus.NOT_FOUND,
            )
            return
        nodes, edges = _build_claim_lineage(self._server.service, target)
        self._write_html(_render_claim_lineage_html(target, nodes, edges))

    def _write_dashboard(self) -> None:
        html = dashboard_template.DASHBOARD_HTML
        html = session_scope_surface.hydrate_dashboard_html(graph_observations_surface.hydrate_dashboard_html(capture_inbox_surface.hydrate_dashboard_html(html, escape(_package_version()))))
        html = html.replace("__DECISION_SUMMARY__", dashboard_summary.SUMMARY_HTML).replace("__DECISION_SUMMARY_JS__", dashboard_summary.SUMMARY_JS)
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _triage_flags(self, limit: int) -> dict[int, dict[str, bool]]:
        return triage_flags(self._server.service, limit)

    def _handle_claims(self, query_string: str) -> None:
        query = parse_qs(query_string)
        limit = _parse_int(_first_query_value(query, "limit"), default=50, minimum=1, maximum=500)
        include_archived = _parse_bool(_first_query_value(query, "include_archived"), default=False)
        allow_sensitive = _parse_bool(_first_query_value(query, "allow_sensitive"), default=False)
        status = _first_query_value(query, "status")
        self._write_json(claims_payload(
            self._server.service, status=status, limit=limit,
            include_archived=include_archived, allow_sensitive=allow_sensitive,
            serialize=_claim_to_dict,
        ))

    def _handle_events(self, query_string: str) -> None:
        query = parse_qs(query_string)
        limit = _parse_int(_first_query_value(query, "limit"), default=100, minimum=1, maximum=1000)
        claim_id_raw = _first_query_value(query, "claim_id")
        event_type = _first_query_value(query, "event_type")
        claim_id = int(claim_id_raw) if claim_id_raw is not None else None
        if claim_id is not None and claim_id <= 0:
            raise ValueError("claim_id must be positive")
        self._write_json(events_payload(
            self._server.service, limit=limit, claim_id=claim_id,
            event_type=event_type, serialize=_event_to_dict,
        ))

    def _handle_timeline(self, query_string: str) -> None:
        query = parse_qs(query_string)
        limit = _parse_int(_first_query_value(query, "limit"), default=100, minimum=1, maximum=2000)
        event_type = _first_query_value(query, "event_type")
        self._write_json(events_payload(
            self._server.service, limit=limit, claim_id=None,
            event_type=event_type, serialize=_event_to_dict, output_key="timeline",
        ))

    def _handle_conflicts(self, query_string: str) -> None:
        query = parse_qs(query_string)
        limit = _parse_int(_first_query_value(query, "limit"), default=50, minimum=1, maximum=500)
        include_stale = _parse_bool(_first_query_value(query, "include_stale"), default=False)
        self._write_json(conflicts_payload(
            self._server.service, limit=limit, include_stale=include_stale,
            serialize=_claim_to_dict,
        ))

    def _handle_review_queue(self, query_string: str) -> None:
        query = parse_qs(query_string)
        limit = _parse_int(_first_query_value(query, "limit"), default=100, minimum=1, maximum=1000)
        include_stale = _parse_bool(_first_query_value(query, "include_stale"), default=True)
        include_conflicted = _parse_bool(_first_query_value(query, "include_conflicted"), default=True)
        allow_sensitive = _parse_bool(_first_query_value(query, "allow_sensitive"), default=False)
        exclude_reviewed = _parse_bool(_first_query_value(query, "exclude_reviewed"), default=False)
        exclude_suppressed = _parse_bool(_first_query_value(query, "exclude_suppressed"), default=False)
        self._write_json(review_queue_payload(
            self._server.service, limit=limit, include_stale=include_stale,
            include_conflicted=include_conflicted, allow_sensitive=allow_sensitive,
            exclude_reviewed=exclude_reviewed, exclude_suppressed=exclude_suppressed,
        ))

    def _handle_mobile_review_queue(self, query_string: str) -> None:
        query = parse_qs(query_string)
        limit = _parse_review_queue_limit(_first_query_value(query, "n"))
        scope = _first_query_value(query, "scope")
        cursor = _parse_review_queue_cursor(_first_query_value(query, "cursor"))
        self._write_json(mobile_review_queue_payload(
            self._server.service, limit=limit, scope=scope, cursor=cursor,
        ))

    def _handle_action_proposals(self, query_string: str) -> None:
        query = parse_qs(query_string)
        limit = _parse_int(_first_query_value(query, "limit"), default=100, minimum=1, maximum=500)
        status = _first_query_value(query, "status")
        destination = _first_query_value(query, "destination")
        self._write_json(action_proposals_payload(
            self._server.service, status=status, destination=destination, limit=limit,
        ))

    def _handle_atlas_version(self, query_string: str) -> None:
        from memorymaster.bridges.atlas_contract import atlas_contract_payload

        payload = atlas_contract_payload()
        self._write_json({"ok": True, **payload})

    def _handle_action_proposal_status(self, payload: dict[str, Any]) -> None:
        self._write_json(update_action_proposal_status(self._server.service, payload))

    def _handle_retrieval(self, query_string: str) -> None:
        params = _parse_retrieval_query_params(query_string)
        text = params["text"]
        mode = params["mode"]
        limit = params["limit"]
        include_stale = params["include_stale"]
        include_conflicted = params["include_conflicted"]
        allow_sensitive = params["allow_sensitive"]
        scope_allowlist = params["scope_allowlist"]

        attempts_with_scope = _build_retrieval_method_attempts(
            limit, include_stale, include_conflicted, mode, allow_sensitive, scope_allowlist, with_scope=True
        )

        query_rows_fn = getattr(self._server.service, "query_rows", None)
        claims: list[Any] = []
        scored_by_claim_id: dict[int, dict[str, Any]] = {}
        if callable(query_rows_fn):
            rows_data = _call_retrieval_method_with_fallback(query_rows_fn, text, attempts_with_scope)
            claims, scored_by_claim_id = _extract_claims_from_rows(rows_data)

        if not callable(query_rows_fn):
            claims = _call_retrieval_method_with_fallback(self._server.service.query, text, attempts_with_scope)
        if scope_allowlist:
            claims = [claim for claim in claims if claim.scope in scope_allowlist]
        triage_flags = self._triage_flags(max(limit * 30, 200))
        q_tokens = {token for token in text.lower().split() if token.strip()}
        rows: list[dict[str, Any]] = [
            _compute_claim_row(claim, q_tokens, scored_by_claim_id, triage_flags)
            for claim in claims
        ]
        self._write_json({"ok": True, "rows": len(rows), "rows_data": rows, "query": text, "mode": mode, "scope_allowlist": scope_allowlist})

    def _handle_recall_analysis(self, query_string: str) -> None:
        """Read-only ranking explainability: per-claim score breakdown, the
        active retrieval weights/profile, and per-component claim rankings.

        Thin wrapper over ``service.recall_analysis`` — no ranking math here.
        Returns an empty analysis if the service lacks the method (older build).
        """
        params = _parse_retrieval_query_params(query_string)
        # Introspection endpoint: default to including candidates so operators
        # can debug how unverified claims would rank (parity with CLI/MCP).
        include_candidates = _parse_bool(
            _first_query_value(parse_qs(query_string), "include_candidates"), default=True
        )
        analyze = getattr(self._server.service, "recall_analysis", None)
        if not callable(analyze):
            self._write_json({"ok": True, "rows": 0, "results": [], "query": params["text"]})
            return
        analysis = analyze(
            query_text=params["text"],
            limit=params["limit"],
            retrieval_mode=params["mode"],
            include_stale=params["include_stale"],
            include_conflicted=params["include_conflicted"],
            include_candidates=include_candidates,
            allow_sensitive=params["allow_sensitive"],
            scope_allowlist=params["scope_allowlist"] or None,
        )
        self._write_json({"ok": True, **analysis})

    def _handle_audit(self, query_string: str) -> None:
        query = parse_qs(query_string)
        limit = _parse_int(_first_query_value(query, "limit"), default=50, minimum=1, maximum=1000)
        self._write_json(audit_payload(self._server.service, limit=limit, serialize=_event_to_dict))

    def _handle_namespaces(self, query_string: str) -> None:
        query = parse_qs(query_string)
        limit = _parse_int(_first_query_value(query, "limit"), default=200, minimum=1, maximum=5000)
        self._write_json(namespaces_payload(self._server.service, limit=limit))

    def _handle_provenance(self, query_string: str) -> None:
        """Per-agent provenance: ingest counts + status mix by source_agent.

        Reliable post-P3 intake policy (source_agent is default-tagged/enforced)
        and cheap via idx_claims_source_agent. Read-only GROUP BY over claims.
        Recall is NOT row-attributed (the events table has no source_agent), so
        recall_total is surfaced separately as a best-effort in-process counter
        and labelled as such — we do not fabricate a per-agent recall split."""
        rows = _provenance_rows(self._server.service)
        self._write_json(
            {
                "ok": True,
                "rows": len(rows),
                "agents": rows,
                "attribution": _ingest_recall_note(),
            }
        )

    def _handle_session_stats(self, query_string: str) -> None:
        query = parse_qs(query_string)
        limit = _parse_int(_first_query_value(query, "limit"), default=2000, minimum=1, maximum=20000)
        rows = _tail_events_from_jsonl(self._server.operator_log_jsonl, limit)
        event_counts: Counter[str] = Counter()
        tool_counts: Counter[str] = Counter()
        sessions: set[str] = set()
        threads: set[str] = set()
        for row in rows:
            event_counts[str(row.get("event") or "message")] += 1
            session = str(row.get("session_id") or "").strip()
            thread = str(row.get("thread_id") or "").strip()
            if session:
                sessions.add(session)
            if thread:
                threads.add(thread)
            tool = str(row.get("tool") or row.get("tool_name") or "").strip()
            if tool:
                tool_counts[tool] += 1
        latency_stats = _latency_summary_from_rows(rows)
        self._write_json({"ok": True, "summary": {"rows_scanned": len(rows), "sessions": len(sessions), "threads": len(threads), "event_counts": dict(event_counts), "tool_counts": dict(tool_counts), "latency_ms": latency_stats}})

    def _handle_observability(self, query_string: str) -> None:
        query = parse_qs(query_string)
        log_limit = _parse_int(_first_query_value(query, "log_limit"), default=1500, minimum=1, maximum=20000)
        event_limit = _parse_int(_first_query_value(query, "event_limit"), default=600, minimum=1, maximum=10000)
        queue_limit = _parse_int(_first_query_value(query, "queue_limit"), default=250, minimum=1, maximum=2000)

        log_rows = _tail_events_from_jsonl(self._server.operator_log_jsonl, log_limit)
        log_event_counts: Counter[str] = Counter()
        tool_counts: Counter[str] = Counter()
        sessions: set[str] = set()
        threads: set[str] = set()
        for row in log_rows:
            log_event_counts[str(row.get("event") or "message")] += 1
            session = str(row.get("session_id") or "").strip()
            thread = str(row.get("thread_id") or "").strip()
            if session:
                sessions.add(session)
            if thread:
                threads.add(thread)
            tool = str(row.get("tool") or row.get("tool_name") or "").strip()
            if tool:
                tool_counts[tool] += 1

        recent_events = self._server.service.list_events(limit=event_limit)
        event_counts: Counter[str] = Counter(str(event.event_type or "event") for event in recent_events)

        queue_items = build_review_queue(
            self._server.service,
            limit=queue_limit,
            include_stale=True,
            include_conflicted=True,
            include_sensitive=False,
        )
        triage_flags = self._triage_flags(max(queue_limit * 20, 200))
        status_counts: Counter[str] = Counter()
        triage_reviewed = 0
        triage_suppressed = 0
        actionable = 0
        for item in queue_items:
            status_counts[str(item.status or "unknown")] += 1
            triage = triage_flags.get(int(item.claim_id), {"reviewed": False, "suppressed": False})
            reviewed = bool(triage["reviewed"])
            suppressed = bool(triage["suppressed"])
            if reviewed:
                triage_reviewed += 1
            if suppressed:
                triage_suppressed += 1
            if not reviewed and not suppressed:
                actionable += 1

        top_queue = []
        for item in queue_items[:5]:
            triage = triage_flags.get(int(item.claim_id), {"reviewed": False, "suppressed": False})
            top_queue.append(
                {
                    "claim_id": int(item.claim_id),
                    "status": str(item.status or "unknown"),
                    "priority": float(item.priority),
                    "reason": str(item.reason or ""),
                    "reviewed": bool(triage["reviewed"]),
                    "suppressed": bool(triage["suppressed"]),
                }
            )

        self._write_json(
            {
                "ok": True,
                "observability": {
                    "operator": {
                        **self._server.operator_status(),
                        "rows_scanned": len(log_rows),
                        "sessions": len(sessions),
                        "threads": len(threads),
                        "event_counts": dict(log_event_counts),
                        "tool_counts": dict(tool_counts),
                        "latency_ms": _latency_summary_from_rows(log_rows),
                    },
                    "events_recent": {
                        "rows_scanned": len(recent_events),
                        "event_counts": dict(event_counts),
                    },
                    "queue": {
                        "rows_scanned": len(queue_items),
                        "status_counts": dict(status_counts),
                        "actionable": actionable,
                        "triage_reviewed": triage_reviewed,
                        "triage_suppressed": triage_suppressed,
                        "top": top_queue,
                    },
                },
            }
        )

    def _handle_integrity(self, query_string: str) -> None:
        # Reliability panel (P1 spec §2.10): WAL/spool/drift/busy metrics —
        # the §5 flip criteria the operator checks at day 7.
        from memorymaster.surfaces.dashboard_integrity import build_integrity_panel

        self._write_json({"ok": True, "integrity": build_integrity_panel(self._server.service)})

    def _handle_validation_latency(self, query_string: str) -> None:
        self._write_json(_validation_latency_metric(self._server.service))

    def _handle_triage_action(self, payload: dict[str, Any]) -> None:
        self._write_json(apply_triage_action(
            self._server.service, payload, serialize_claim=_claim_to_dict,
        ))

    def _handle_operator_control(self, payload: dict[str, Any]) -> None:
        self._write_json(control_operator(self._server, payload))

    def _send_sse_event(self, record: dict[str, Any]) -> bool:
        """Send a server-sent event record. Returns False if connection is closed."""
        chunk = f"event: {str(record.get('event') or 'message')}\ndata: {json.dumps(record, ensure_ascii=True)}\n\n".encode("utf-8")
        try:
            self.wfile.write(chunk)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return False
        return True

    def _follow_stream(self, log_path: Path) -> None:
        """Follow new events from the log file and send them to the client."""
        offset = log_path.stat().st_size if log_path.exists() else 0
        while True:
            if log_path.exists():
                with log_path.open("r", encoding="utf-8") as handle:
                    handle.seek(offset)
                    while True:
                        raw = handle.readline()
                        if not raw:
                            break
                        offset = handle.tell()
                        line = raw.strip().lstrip("\ufeff")
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(record, dict) and not self._send_sse_event(record):
                            return
            time.sleep(0.25)

    def _handle_operator_stream(self, query_string: str) -> None:
        query = parse_qs(query_string)
        last = _parse_int(_first_query_value(query, "last"), default=20, minimum=0, maximum=2000)
        follow = _parse_bool(_first_query_value(query, "follow"), default=True)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        log_path = self._server.operator_log_jsonl
        for event in _tail_events_from_jsonl(log_path, last):
            if not self._send_sse_event(event):
                return
        if not follow:
            return
        self._follow_stream(log_path)


def create_dashboard_server(
    *,
    db_target: str | Path | None = None,
    service: MemoryService | None = None,
    workspace_root: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    operator_log_jsonl: str | Path = "artifacts/operator/operator_events.jsonl",
) -> DashboardHTTPServer:
    # v3.19.0-H2: refuse non-loopback bind without an auth secret unless the
    # operator explicitly opts in. Raises dashboard_auth.BindUnsafeError on
    # an unsafe configuration; callers should let it propagate so the
    # mistake is visible at startup instead of silently exposed.
    dashboard_auth.check_bind_safety(host)

    if service is None:
        if db_target is None:
            db_target = "memorymaster.db"
        service = MemoryService(db_target, workspace_root=workspace_root)
    else:
        if db_target is None:
            db_target = getattr(service.store, "db_path", None) or "memorymaster.db"
    return DashboardHTTPServer((host, int(port)), service=service, operator_log_jsonl=operator_log_jsonl, db_target=db_target, workspace_root=workspace_root)


def run_dashboard(
    *,
    db_target: str | Path = "memorymaster.db",
    workspace_root: str | Path = ".",
    host: str = "127.0.0.1",
    port: int = 8765,
    operator_log_jsonl: str | Path = "artifacts/operator/operator_events.jsonl",
) -> None:
    server = create_dashboard_server(db_target=db_target, workspace_root=workspace_root, host=host, port=port, operator_log_jsonl=operator_log_jsonl)
    print(f"memorymaster dashboard listening on http://{host}:{port}/dashboard")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run MemoryMaster dashboard")
    parser.add_argument("--db", default="memorymaster.db", help="SQLite path or Postgres DSN")
    parser.add_argument("--workspace", default=".", help="Workspace root")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8765, help="Bind port")
    parser.add_argument("--operator-log-jsonl", default="artifacts/operator/operator_events.jsonl", help="Operator events path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    run_dashboard(db_target=args.db, workspace_root=args.workspace, host=args.host, port=args.port, operator_log_jsonl=args.operator_log_jsonl)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
