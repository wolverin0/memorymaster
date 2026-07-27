"""Capture Inbox read model for the local dashboard.

It joins source items, capture jobs, evidence, governed claims, citations, and
supported entity relationships without changing lifecycle state. Sensitive
evidence is represented by a marker, not its text. Source retirement remains
the public facade's preview/apply operation.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any


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
        output.append(
            {
                "id": claim.id,
                "text": claim.text,
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
