from __future__ import annotations

import base64
import json
from typing import Any


def _encode_cursor(values: list[object]) -> str:
    raw = json.dumps(values, separators=(",", ":"), ensure_ascii=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str, size: int) -> list[Any]:
    if not cursor:
        return []
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        values = json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid pagination cursor") from exc
    if not isinstance(values, list) or len(values) != size:
        raise ValueError("invalid pagination cursor")
    return values


class _PaginationMixin:
    def list_claims_page(
        self,
        *,
        limit: int,
        cursor: str = "",
        status: str | None = None,
        include_archived: bool = False,
        include_citations: bool = False,
        scope_allowlist: list[str] | None = None,
        tenant_id: str | None = None,
        holder: str | None = None,
    ):
        clauses, params = self._build_list_clauses(
            status, None, include_archived, scope_allowlist, tenant_id, holder
        )
        after = _decode_cursor(cursor, 4)
        if after:
            pinned, confidence, updated_at, claim_id = after
            clauses.append(
                "(pinned < ? OR (pinned = ? AND confidence < ?) OR "
                "(pinned = ? AND confidence = ? AND updated_at < ?) OR "
                "(pinned = ? AND confidence = ? AND updated_at = ? AND id < ?))"
            )
            params.extend(
                [
                    pinned,
                    pinned,
                    confidence,
                    pinned,
                    confidence,
                    updated_at,
                    pinned,
                    confidence,
                    updated_at,
                    claim_id,
                ]
            )
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)) + 1)
        sql = (
            f"SELECT * FROM claims {where_sql} "
            "ORDER BY pinned DESC, confidence DESC, updated_at DESC, id DESC LIMIT ?"
        )
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        claims = [self._row_to_claim(row) for row in rows]
        if include_citations and claims:
            citation_map = self.list_citations_batch([claim.id for claim in claims])
            for claim in claims:
                claim.citations = citation_map.get(claim.id, [])
        next_cursor = ""
        if has_more and rows:
            row = rows[-1]
            next_cursor = _encode_cursor(
                [int(row["pinned"]), float(row["confidence"]), row["updated_at"], int(row["id"])]
            )
        return claims, next_cursor

    def list_events_page(
        self,
        *,
        limit: int,
        cursor: str = "",
        claim_id: int | None = None,
        event_type: str | None = None,
    ):
        clauses: list[str] = []
        params: list[object] = []
        if claim_id is not None:
            clauses.append("claim_id = ?")
            params.append(claim_id)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        after = _decode_cursor(cursor, 2)
        if after:
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([after[0], after[0], after[1]])
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)) + 1)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM events {where_sql} "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        events = [self._row_to_event(row) for row in rows]
        next_cursor = ""
        if has_more and rows:
            next_cursor = _encode_cursor([rows[-1]["created_at"], int(rows[-1]["id"])])
        return events, next_cursor
