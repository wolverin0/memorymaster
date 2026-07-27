"""Capture Inbox read model for the local dashboard.

It joins source items, capture jobs, evidence, governed claims, citations, and
supported entity relationships without changing lifecycle state. Sensitive
evidence is represented by a marker, not its text. Source retirement remains
the public facade's preview/apply operation.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs


CAPTURE_INBOX_SECTION_HTML = """<section class="wide">
<div class="section-head"><div class="icon icon-blue">&#128229;</div><div><h2>Capture Inbox</h2><div class="desc">Sources, processing jobs, evidence, governed claims, and supported relationships</div></div></div>
<div id="capture-status" class="muted" role="status" aria-live="polite"></div>
<div id="capture-inbox" class="scroll"><div class="empty">No captures yet</div></div>
</section>"""

CAPTURE_INBOX_FUNCTIONS_JS = """
function captureBadge(j){return '<span class="badge badge-'+esc(j.status||'archived')+'">'+esc(j.stage||'job')+': '+esc(j.status||'unknown')+'</span>'+(j.error_code?'<span class="muted"> '+esc(j.error_code)+'</span>':'');}
function fillCapture(d){const rows=Array.isArray(d.items)?d.items:[];const box=document.getElementById('capture-inbox');if(!rows.length){box.innerHTML='<div class="empty">No captures yet</div>';return;}box.innerHTML=rows.map(i=>{const jobs=(i.jobs||[]).map(captureBadge).join(' ')||'<span class="muted">no jobs</span>';const ev=(i.evidence||[]).map(e=>'<div><span class="mono">evidence #'+esc(e.id)+'</span> '+esc(e.evidence_type)+': '+esc(e.excerpt||'-')+'</div>').join('')||'<div class="muted">awaiting evidence</div>';const claims=(i.claims||[]).map(c=>'<div>'+statusBadge(c.status)+' <span class="mono">#'+esc(c.id)+'</span> '+esc(c.text||'-')+' <span class="muted">('+esc((c.citations||[]).length)+' citations)</span></div>').join('')||'<div class="muted">no derived claims</div>';const rels=(i.relationships||[]).map(r=>'<div class="mono">'+esc(r.source)+' '+esc(r.relation)+' '+esc(r.target)+' &#8592; claim #'+esc(r.supporting_claim_id)+'</div>').join('')||'<div class="muted">no supported relationships</div>';const retired=i.retired_at?'<span class="badge badge-archived">retired</span>':'<button data-capture-retire="'+esc(i.id)+'">Preview retirement</button>';return '<div class="card" data-source-id="'+esc(i.id)+'"><div class="card-head"><strong>'+esc(i.display_name||i.source_type)+'</strong> <span class="mono">#'+esc(i.id)+'</span> '+esc(i.item_type)+' '+retired+'</div><div class="card-row"><strong>Processing:</strong> '+jobs+'</div><div class="card-row"><strong>Evidence:</strong> '+ev+'</div><div class="card-row"><strong>Claims:</strong> '+claims+'</div><div class="card-row"><strong>Relationships:</strong> '+rels+'</div></div>';}).join('');}
async function refreshCapture(){fillCapture(await jget('/api/capture-inbox?limit=30'));}
"""

CAPTURE_INBOX_EVENTS_JS = """document.getElementById('capture-inbox').addEventListener('click',async(ev)=>{const t=ev.target;if(!t||!t.hasAttribute('data-capture-retire'))return;const id=Number(t.getAttribute('data-capture-retire'));const status=document.getElementById('capture-status');try{const preview=await jpost('/api/capture-inbox/retire',{source_item_id:id,apply:false});const summary=(preview.actions||[]).map(a=>a.claim_id?('#'+a.claim_id+' '+a.from_status+' -> '+a.to_status):'retire source').join('; ');status.innerHTML='Preview: '+esc(summary)+' <button id="capture-apply-retire" class="danger">Apply retirement</button>';document.getElementById('capture-apply-retire').addEventListener('click',async()=>{await jpost('/api/capture-inbox/retire',{source_item_id:id,apply:true});status.textContent='Source '+id+' retired; evidence and audit history preserved.';await refreshCapture();},{once:true});}catch(error){status.textContent='Retirement preview failed: '+String((error&&error.message)||error);}});"""


def _row_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _execute(conn: Any, postgres: bool, sql: str, params: tuple[Any, ...]) -> Any:
    if postgres:
        cursor = conn.cursor()
        cursor.execute(sql.replace("?", "%s"), params)
        return cursor
    return conn.execute(sql, params)


def _safe_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        payload = raw
    else:
        try:
            payload = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
    if not isinstance(payload, dict):
        return {}
    keys = ("locator", "source_uri", "content_type", "scope", "provider_kind")
    return {key: payload[key] for key in keys if key in payload}


def _evidence_rows(conn: Any, postgres: bool, source_id: int) -> list[dict[str, Any]]:
    rows = _execute(
        conn,
        postgres,
        """SELECT id, evidence_type, text, media_path, provider, confidence,
                  sensitivity, content_hash, created_at
           FROM evidence_items WHERE source_item_id=? ORDER BY id""",
        (source_id,),
    ).fetchall()
    output = []
    for row in rows:
        item = _row_dict(row)
        sensitive = str(item.get("sensitivity") or "none") in {"high", "redacted"}
        item["excerpt"] = "[sensitive evidence]" if sensitive else str(item.pop("text") or "")[:240]
        item.pop("text", None)
        output.append(item)
    return output


def _job_rows(conn: Any, postgres: bool, source_id: int) -> list[dict[str, Any]]:
    rows = _execute(
        conn,
        postgres,
        """SELECT id, stage, status, attempts, next_attempt_at, error_code,
                  error_detail, created_at, updated_at, completed_at
           FROM capture_jobs WHERE source_item_id=? ORDER BY id""",
        (source_id,),
    ).fetchall()
    return [_row_dict(row) for row in rows]


def _claim_rows(service: Any, conn: Any, postgres: bool, source_id: int) -> list[dict[str, Any]]:
    rows = _execute(
        conn,
        postgres,
        """SELECT DISTINCT c.id FROM claims c
           JOIN claim_evidence_links cel ON cel.claim_id=c.id
           JOIN evidence_items e ON e.id=cel.evidence_item_id
           WHERE e.source_item_id=? ORDER BY c.id""",
        (source_id,),
    ).fetchall()
    output = []
    for row in rows:
        claim = service.store.get_claim(int(row["id"]), include_citations=True)
        if claim is None:
            continue
        sensitive = claim.visibility in {"high", "redacted", "sensitive"}
        output.append(
            {
                "id": claim.id,
                "text": "[sensitive claim]" if sensitive else claim.text,
                "status": claim.status,
                "scope": claim.scope,
                "visibility": claim.visibility,
                "citations": [asdict(citation) for citation in claim.citations],
            }
        )
    return output


def _relationship_rows(
    conn: Any, postgres: bool, claim_ids: list[int]
) -> list[dict[str, Any]]:
    if not claim_ids:
        return []
    marks = ",".join("?" for _ in claim_ids)
    rows = _execute(
        conn,
        postgres,
        f"""SELECT se.canonical_name AS source, te.canonical_name AS target,
                   es.relation, es.supporting_claim_id, es.scope,
                   es.ontology_version
            FROM entity_edge_supports es
            JOIN entities se ON se.id=es.source_entity_id
            JOIN entities te ON te.id=es.target_entity_id
            WHERE es.supporting_claim_id IN ({marks})
            ORDER BY es.supporting_claim_id, es.relation""",
        tuple(claim_ids),
    ).fetchall()
    return [_row_dict(row) for row in rows]


def _source_row(service: Any, conn: Any, postgres: bool, row: Any) -> dict[str, Any]:
    source = _row_dict(row)
    source_id = int(source["id"])
    claims = _claim_rows(service, conn, postgres, source_id)
    return {
        **source,
        "payload": _safe_payload(source.pop("payload_json", None)),
        "jobs": _job_rows(conn, postgres, source_id),
        "evidence": _evidence_rows(conn, postgres, source_id),
        "claims": claims,
        "relationships": _relationship_rows(
            conn, postgres, [int(claim["id"]) for claim in claims]
        ),
    }


def capture_inbox_payload(service: Any, *, limit: int = 50) -> dict[str, Any]:
    """Return newest capture sources with their complete governed lineage."""
    store = service.store
    postgres = hasattr(store, "dsn")
    with store.connect() as conn:
        rows = _execute(
            conn,
            postgres,
            """SELECT s.id, s.source_item_id, s.item_type, s.occurred_at,
                      s.payload_json, s.content_hash, s.sensitivity, s.created_at,
                      s.updated_at, s.retired_at, s.retirement_reason,
                      x.source_type, x.display_name
               FROM source_items s
               JOIN external_sources x ON x.id=s.source_id
               ORDER BY s.id DESC LIMIT ?""",
            (min(max(limit, 1), 100),),
        ).fetchall()
        items = [_source_row(service, conn, postgres, row) for row in rows]
    return {"ok": True, "rows": len(items), "items": items}


def hydrate_dashboard_html(html: str, version: str) -> str:
    """Insert the Capture Inbox assets into the dashboard template."""
    return (
        html.replace("__MEMORYMASTER_VERSION__", version)
        .replace("__CAPTURE_INBOX_SECTION__", CAPTURE_INBOX_SECTION_HTML)
        .replace("__CAPTURE_INBOX_FUNCTIONS__", CAPTURE_INBOX_FUNCTIONS_JS)
        .replace("__CAPTURE_INBOX_EVENTS__", CAPTURE_INBOX_EVENTS_JS)
    )


def write_favicon_response(handler: Any, _query_string: str) -> None:
    """Return an empty favicon response without polluting the browser console."""
    handler.send_response(HTTPStatus.NO_CONTENT)
    handler.end_headers()


def write_capture_inbox_response(handler: Any, query_string: str) -> None:
    """Write the bounded Capture Inbox JSON read model."""
    query = parse_qs(query_string)
    raw_limit = str((query.get("limit") or ["50"])[-1]).strip()
    limit = int(raw_limit or "50")
    if limit < 1 or limit > 100:
        raise ValueError("Expected integer in range [1, 100]")
    handler._write_json(capture_inbox_payload(handler._server.service, limit=limit))


def write_capture_retirement_response(handler: Any, payload: dict[str, Any]) -> None:
    """Preview or apply governed source retirement from the local dashboard."""
    from memorymaster.public.v1 import forget

    source_item_id = payload.get("source_item_id")
    apply = payload.get("apply", False)
    if isinstance(source_item_id, bool) or not isinstance(source_item_id, int):
        raise ValueError("source_item_id must be a positive integer")
    if source_item_id <= 0 or not isinstance(apply, bool):
        raise ValueError("invalid source retirement request")
    receipt = forget(
        source_item_id=source_item_id,
        apply=apply,
        db=handler._server.db_target,
        workspace=handler._server.workspace_root,
    )
    handler._write_json({"ok": True, **asdict(receipt)})
