"""Shared bounded detection helpers for the legacy sensitivity inventory."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import quote

from memorymaster.core.security import scan_persisted_value

_SQL_QUOTES = {'"': '"', "'": "'", "`": "`", "[": "]"}
_REASONS = frozenset(
    {
        "sensitive_value",
        "embedding_invalid",
        "embedding_string",
        "binary_opaque",
        "scan_incomplete",
    }
)
_TABLES = {
    "claims": "claims",
    "citations": "citations",
    "events": "events",
    "claim_embeddings": "claim_embeddings",
    "verbatim": "verbatim",
    "verbatim_memories": "verbatim",
    "feedback": "feedback",
    "usage_feedback": "feedback",
    "external_sources": "atlas.source",
    "source_items": "atlas.source",
    "evidence_items": "atlas.evidence",
    "action_proposals": "atlas.action",
    "media_retry_queue": "atlas.media_retry",
    "query_cache": "cache",
    "cache_meta": "cache",
    "miner_state": "miner",
}
_SAFE_FIELDS = frozenset(
    {
        "id",
        "claim_id",
        "human_id",
        "text",
        "normalized_text",
        "metadata",
        "subject",
        "predicate",
        "object_value",
        "holder",
        "source_agent",
        "idempotency_key",
        "scope",
        "claim_type",
        "volatility",
        "status",
        "visibility",
        "tenant_id",
        "created_at",
        "updated_at",
        "event_time",
        "valid_from",
        "valid_until",
        "wiki_path",
        "wiki_article",
        "pinned",
        "confidence",
        "embedding_json",
        "model",
        "blob",
        "source",
        "locator",
        "excerpt",
        "event_type",
        "from_status",
        "to_status",
        "details",
        "payload",
        "payload_json",
        "config",
        "config_json",
        "content_hash",
        "payload_hash",
        "hash_algorithm",
        "session_id",
        "thread_id",
        "role",
        "content",
        "sync_status",
        "identity",
        "canonical_identity",
        "sender",
        "sender_id",
        "sender_type",
        "source_type",
        "external_id",
        "sensitivity",
        "media",
        "media_id",
        "media_url",
        "media_path",
        "provider",
        "provider_data",
        "title",
        "description",
        "destination",
        "destination_json",
        "external_ref",
        "error",
        "last_error",
        "query_text",
        "response_json",
        "state_json",
        "rule_json",
        "entity_name",
        "entity_type",
        "link_type",
        "timeline_json",
    }
)
_MAX_SCAN_TEXT_BYTES = 4 * 1024 * 1024
_MAX_SCAN_NODES = 10_000
_MAX_SCAN_DEPTH = 128
_MAX_JSON_TEXT_BYTES = 64 * 1024
_MAX_JSON_DIGITS = 1024
_JSON_INVALID = object()
_JSON_UNSAFE = object()


def _json_text_unsafe(value: str) -> bool:
    if len(value.encode("utf-8", errors="ignore")) > _MAX_JSON_TEXT_BYTES:
        return True
    digits = 0
    for character in value:
        digits = digits + 1 if character in "0123456789" else 0
        if digits > _MAX_JSON_DIGITS:
            return True
    return False


def _bounded_json(value: str) -> object:
    if _json_text_unsafe(value):
        return _JSON_UNSAFE
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return _JSON_INVALID
    except (RecursionError, ValueError):
        return _JSON_UNSAFE


def _category(table: str) -> str:
    lowered = table.lower()
    if lowered in _TABLES:
        return _TABLES[lowered]
    for prefix, category in (
        ("rule", "rules"),
        ("entity", "entities"),
        ("link", "entities"),
        ("timeline", "timeline"),
    ):
        if lowered.startswith(prefix):
            return category
    return "unknown"


def _surface(
    table: str,
    column: str,
    declared_type: object,
    value: object,
) -> str:
    lowered = column.lower()
    declared = str(declared_type or "").upper()
    if lowered in _SAFE_FIELDS:
        field = lowered
    elif "embedding" in lowered or "vector" in lowered:
        field = "field_embedding"
    elif isinstance(value, bytes) or "BLOB" in declared:
        field = "field_binary"
    elif (isinstance(value, (int, float)) and not isinstance(value, bool)) or any(
        token in declared for token in ("INT", "REAL", "NUM", "DEC", "FLOA", "DOUB")
    ):
        field = "field_numeric"
    elif lowered.endswith("_json") or "JSON" in declared:
        field = "field_json"
    elif any(token in declared for token in ("CHAR", "CLOB", "TEXT")):
        field = "field_text"
    elif isinstance(value, str):
        parsed = _bounded_json(value)
        field = "field_json" if parsed is not _JSON_INVALID and parsed is not _JSON_UNSAFE else "field_text"
    else:
        field = "field_other"
    return f"{_category(table)}.{field}"


def _numeric_tree(value: object) -> bool:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, (int, float)) and not isinstance(current, bool):
            continue
        if type(current) is list:
            pending.extend(current)
            continue
        if type(current) is dict:
            pending.extend(current.values())
            continue
        return False
    return True


def _structure_bounded(value: object) -> bool:
    pending: list[tuple[object, int]] = [(value, 0)]
    containers: set[int] = set()
    nodes = 0
    text_bytes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > _MAX_SCAN_NODES or depth > _MAX_SCAN_DEPTH:
            return False
        if current is None or isinstance(current, (bool, int, float)):
            continue
        if isinstance(current, (str, bytes)):
            try:
                text_bytes += len(current.encode("utf-8") if isinstance(current, str) else current)
            except UnicodeEncodeError:
                return False
            if text_bytes > _MAX_SCAN_TEXT_BYTES:
                return False
            continue
        if type(current) not in {dict, list, tuple, set, frozenset}:
            return False
        identity = id(current)
        if identity in containers:
            return False
        containers.add(identity)
        if len(pending) + len(current) > _MAX_SCAN_NODES:
            return False
        if isinstance(current, dict):
            for key, nested in current.items():
                pending.append((key, depth + 1))
                pending.append((nested, depth + 1))
        else:
            pending.extend((nested, depth + 1) for nested in current)
    return True


def _sensitivity_reason(value: object) -> set[str]:
    try:
        return {"sensitive_value"} if scan_persisted_value(value) else set()
    except (RecursionError, TypeError, ValueError):
        return {"scan_incomplete"}


def _embedding_reasons(value: str) -> set[str]:
    reasons = _sensitivity_reason(value)
    parsed = _bounded_json(value)
    if parsed is _JSON_INVALID:
        return reasons | {"embedding_invalid"}
    if parsed is _JSON_UNSAFE or not _structure_bounded(parsed):
        return reasons | {"scan_incomplete"}
    reasons.update(_sensitivity_reason(parsed))
    if not _numeric_tree(parsed):
        reasons.add("embedding_string")
    return reasons


def _reasons(value: object, column: str) -> set[str]:
    if not _structure_bounded(value):
        return {"scan_incomplete"}
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return {"binary_opaque"}
    if isinstance(value, str) and ("embedding" in column.lower() or "vector" in column.lower()):
        return _embedding_reasons(value)
    reasons = _sensitivity_reason(value)
    if not isinstance(value, str):
        return reasons
    parsed = _bounded_json(value)
    if parsed is _JSON_UNSAFE:
        return reasons | {"scan_incomplete"}
    if parsed is not _JSON_INVALID:
        value = parsed
    if not _structure_bounded(value):
        return reasons | {"scan_incomplete"}
    return reasons | _sensitivity_reason(value)


def _record(surfaces: dict[str, dict[str, object]], surface: str, reasons: Iterable[str]) -> None:
    item = surfaces.setdefault(
        surface, {"records_scanned": 0, "records_flagged": 0, "finding_counts": defaultdict(int)}
    )
    item["records_scanned"] = int(item["records_scanned"]) + 1
    found = sorted(set(reasons) & _REASONS)
    if found:
        item["records_flagged"] = int(item["records_flagged"]) + 1
        counts = item["finding_counts"]
        assert isinstance(counts, defaultdict)
        for reason in found:
            counts[reason] += 1


def _ensure_surface(surfaces: dict[str, dict[str, object]], surface: str) -> None:
    surfaces.setdefault(
        surface,
        {
            "records_scanned": 0,
            "records_flagged": 0,
            "finding_counts": defaultdict(int),
        },
    )


def _freeze_surfaces(surfaces: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
    return {
        key: {
            "finding_counts": dict(sorted(item["finding_counts"].items())),
            "records_flagged": item["records_flagged"],
            "records_scanned": item["records_scanned"],
        }
        for key, item in sorted(surfaces.items())
    }


def _sqlite_uri(path: Path) -> str:
    return f"file:{quote(path.resolve().as_posix())}?mode=ro"


def _empty_sqlite_result() -> dict[str, object]:
    return {
        "columns_accounted": 0,
        "derived_columns_accounted": 0,
        "derived_records": 0,
        "derived_tables_accounted": 0,
        "derived_views_accounted": 0,
        "records_flagged": 0,
        "records_scanned": 0,
        "scan_incomplete": 0,
        "schema_definition_sensitive": 0,
        "schema_identifier_sensitive": 0,
        "surfaces": {},
        "tables_accounted": 0,
    }


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class _TableListUnavailable(sqlite3.Error):
    pass


def _table_columns(conn: sqlite3.Connection, quoted: str) -> list[tuple[str, object, int]]:
    try:
        rows = list(conn.execute(f"PRAGMA table_xinfo({quoted})"))
    except sqlite3.Error:
        rows = []
    if rows:
        return [(str(row[1]), row[2], int(row[6])) for row in rows]
    rows = list(conn.execute(f"PRAGMA table_info({quoted})"))
    return [(str(row[1]), row[2], 0) for row in rows]


def _quoted_sql_token(sql: str, start: int, closer: str) -> tuple[str, int]:
    token: list[str] = []
    index = start + 1
    while index < len(sql):
        character = sql[index]
        if character == closer:
            if index + 1 < len(sql) and sql[index + 1] == closer:
                token.append(closer)
                index += 2
                continue
            return "".join(token), index + 1
        token.append(character)
        index += 1
    return "".join(token), index


def _sql_tokens(sql: str) -> list[tuple[str, bool]]:
    tokens: list[tuple[str, bool]] = []
    index = 0
    while index < len(sql):
        character = sql[index]
        if character in _SQL_QUOTES:
            token, index = _quoted_sql_token(sql, index, _SQL_QUOTES[character])
            tokens.append((token, True))
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            index = len(sql) if end < 0 else end + 2
            continue
        if character.isalnum() or character == "_":
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            tokens.append((sql[index:end], False))
            index = end
            continue
        if character in {"(", ")", ",", "="}:
            tokens.append((character, False))
        index += 1
    return tokens
