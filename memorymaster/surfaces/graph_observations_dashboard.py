"""Read-only dashboard surface for governed graph observations."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

from memorymaster.knowledge.graph_observation_repository import GraphObservationRepository


GRAPH_OBSERVATIONS_SECTION_HTML = """<section class="wide">
<div class="section-head"><div class="icon icon-purple">&#128376;</div><div><h2>Derived Observations</h2><div class="desc">Opt-in graph synthesis, exact support, diagnostics, and lifecycle history</div></div></div>
<div class="toolbar"><input id="graph-observation-scope" aria-label="Observation scope" placeholder="Exact scope (optional)" style="flex:1"> <button id="graph-observation-refresh">Refresh</button></div>
<div id="graph-observation-status" class="muted" role="status" aria-live="polite"></div>
<div id="graph-observations" class="scroll"><div class="empty">No graph observations yet</div></div>
</section>"""

GRAPH_OBSERVATIONS_FUNCTIONS_JS = """
function fillGraphObservations(d){const rows=Array.isArray(d.observations)?d.observations:[];const jobs=d.jobs||{};const outcomes=d.job_outcomes||{};const diagnostics=Array.isArray(d.diagnostics)?d.diagnostics:[];const box=document.getElementById('graph-observations');document.getElementById('graph-observation-status').textContent='Observations '+rows.length+'; jobs '+JSON.stringify(jobs)+'; completed jobs by outcome '+JSON.stringify(outcomes);const obs=rows.map(o=>{const supports=Array.isArray(o.supports)?o.supports:[];const claims=[...new Set(supports.map(s=>s.supporting_claim_id))];const evidence=[...new Set(supports.map(s=>s.evidence_item_id))];const rels=[...new Set(supports.map(s=>String(s.source_entity_id)+' '+String(s.relation)+' '+String(s.target_entity_id)))];const history=(o.lifecycle||[]).map(e=>'<div class="mono muted">'+esc(e.created_at||'-')+' '+esc(e.event_type||'-')+' '+esc(e.from_status||'')+' &#8594; '+esc(e.to_status||'')+'</div>').join('')||'<div class="muted">no lifecycle events</div>';return '<div class="card"><div class="card-head">'+statusBadge(o.status)+' <strong>'+esc(o.name||o.text||'-')+'</strong> <span class="badge">'+esc(o.observation_type||'-')+'</span> <span class="mono">#'+esc(o.observation_claim_id)+'</span></div><div class="card-row"><strong>Evidence window:</strong> '+esc(o.evidence_window_start||'-')+' &#8594; '+esc(o.evidence_window_end||'-')+'</div><div class="card-row"><strong>Supporting claims:</strong> '+esc(claims.join(', ')||'-')+'</div><div class="card-row"><strong>Evidence:</strong> '+esc(evidence.join(', ')||'-')+'</div><div class="card-row"><strong>Relationships:</strong><div class="mono">'+esc(rels.join(' | ')||'-')+'</div></div><details><summary>Lifecycle history</summary>'+history+'</details></div>';}).join('');const diag=diagnostics.length?'<div class="card"><div class="card-head"><strong>Diagnostics</strong></div>'+diagnostics.map(x=>'<div class="card-row"><span class="badge badge-stale">'+esc(x.stage||'job')+'</span> '+esc(x.error_code||x.outcome||'diagnostic')+' <span class="mono">'+esc(x.diagnostic_codes||'')+'</span> <span class="mono muted">'+esc(x.scope||'-')+' '+esc(x.updated_at||'-')+'</span></div>').join('')+'</div>':'';box.innerHTML=obs+diag||'<div class="empty">No graph observations or diagnostics</div>';}
async function refreshGraphObservations(){const scope=document.getElementById('graph-observation-scope').value||'';fillGraphObservations(await jget('/api/graph-observations?limit=30&scope='+encodeURIComponent(scope)));}
"""

GRAPH_OBSERVATIONS_EVENTS_JS = """document.getElementById('graph-observation-refresh').addEventListener('click',refreshGraphObservations);refreshGraphObservations().catch(e=>showPanelFailure('graph-observations','graph observations',e));"""


def _rows(cursor: Any) -> list[dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in cursor.fetchall()]


def _observation_rows(repository: GraphObservationRepository, conn: Any, *, tenant_id: str | None, scope: str | None, limit: int) -> list[dict[str, Any]]:
    scope_sql = " AND go.scope=?" if scope else ""
    params = (tenant_id, scope, limit) if scope else (tenant_id, limit)
    observations = _rows(
        conn.execute(
            f"""SELECT go.*, c.status, c.text, c.confidence
                FROM graph_observations go JOIN claims c ON c.id=go.observation_claim_id
                WHERE go.tenant_id IS ?{scope_sql}
                ORDER BY go.observation_claim_id DESC LIMIT ?""",
            params,
        )
    )
    for row in observations:
        claim_id = int(row["observation_claim_id"])
        row["supports"] = repository.observation_support_rows(claim_id)
        row["lifecycle"] = _rows(conn.execute(
            """SELECT event_type, from_status, to_status, details, created_at
               FROM events WHERE claim_id=? ORDER BY id""", (claim_id,)))
    return observations


def _diagnostic_rows(conn: Any, *, tenant_id: str | None, scope: str | None) -> list[dict[str, Any]]:
    scope_sql = " AND scope=?" if scope else ""
    params = (tenant_id, scope) if scope else (tenant_id,)
    return _rows(conn.execute(
        f"""SELECT id, scope, stage, status, attempts, error_code, outcome,
                   diagnostic_codes, updated_at FROM graph_observation_jobs
            WHERE tenant_id IS ?{scope_sql}
              AND (error_code IS NOT NULL OR diagnostic_codes IS NOT NULL)
            ORDER BY id DESC LIMIT 30""", params))


def graph_observations_payload(
    service: Any,
    *,
    scope: str | None = None,
    tenant_id: str | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    """Return bounded observation status, exact support, jobs, and history."""
    repository = GraphObservationRepository(service.store)
    with repository._connection() as conn:
        observations = _observation_rows(
            repository, conn, tenant_id=tenant_id, scope=scope, limit=limit
        )
        claim_ids = [int(row["observation_claim_id"]) for row in observations]
        diagnostics = _diagnostic_rows(conn, tenant_id=tenant_id, scope=scope)
    return {
        "ok": True,
        "observations": observations,
        "observation_claim_ids": claim_ids,
        "diagnostics": diagnostics,
        "jobs": repository.status_counts(tenant_id=tenant_id, scope=scope),
        "job_outcomes": repository.outcome_counts(tenant_id=tenant_id, scope=scope),
    }


def hydrate_dashboard_html(html: str) -> str:
    """Insert the graph-observation panel and scripts into the dashboard."""
    return (
        html.replace("__GRAPH_OBSERVATIONS_SECTION__", GRAPH_OBSERVATIONS_SECTION_HTML)
        .replace("__GRAPH_OBSERVATIONS_FUNCTIONS__", GRAPH_OBSERVATIONS_FUNCTIONS_JS)
        .replace("__GRAPH_OBSERVATIONS_EVENTS__", GRAPH_OBSERVATIONS_EVENTS_JS)
    )


def write_graph_observations_response(handler: Any, query_string: str) -> None:
    """Write the bounded, tenant-isolated graph-observation read model."""
    query = parse_qs(query_string)
    raw_limit = str((query.get("limit") or ["30"])[-1]).strip()
    limit = int(raw_limit or "30")
    if limit < 1 or limit > 100:
        raise ValueError("Expected integer in range [1, 100]")
    scope = str((query.get("scope") or [""])[-1]).strip() or None
    handler._write_json(
        graph_observations_payload(
            handler._server.service,
            scope=scope,
            tenant_id=getattr(handler._server.service, "tenant_id", None),
            limit=limit,
        )
    )
