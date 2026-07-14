"""Aggregate-only, snapshot-read legacy sensitivity inventory."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Callable, Iterable, Mapping
from hashlib import sha256
from math import isfinite
from pathlib import Path

from memorymaster.govern.jobs._sensitivity_scan import (
    _MAX_SCAN_DEPTH,
    _MAX_SCAN_NODES,
    _MAX_SCAN_TEXT_BYTES,
    _TableListUnavailable,
    _empty_sqlite_result,
    _ensure_surface,
    _freeze_surfaces,
    _quote_identifier,
    _reasons,
    _record,
    _sql_tokens,
    _surface,
    _table_columns,
)
from memorymaster.stores._storage_shared import connect_ro

_MAX_CHUNK_BYTES = 1024 * 1024
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_ENTRIES = 1_000_000
_MAX_OFFSET_BYTES = 64 * 1024
_MAX_OFFSET_NODES = 4096
_MAX_OFFSET_DEPTH = 64
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_fts5_sql(sql: str) -> bool:
    tokens = _sql_tokens(sql)
    for index, (token, quoted) in enumerate(tokens[:-1]):
        if not quoted and token.casefold() == "using":
            return tokens[index + 1][0].casefold() == "fts5"
    return False


def _fts_has_content_option(sql: str) -> bool:
    tokens = _sql_tokens(sql)
    for index, (token, quoted) in enumerate(tokens[:-1]):
        next_token, next_quoted = tokens[index + 1]
        if not quoted and token.casefold() == "content" and not next_quoted and next_token == "=":
            return True
    return False


def _schema_evidence(
    rows: list[tuple[str, str, str | None]],
    result: dict[str, object],
) -> None:
    identifiers = [_reasons(name, "schema_identifier") for _, name, _ in rows]
    definitions = [_reasons(sql, "schema_definition") for _, _, sql in rows if sql is not None]
    result["schema_identifier_sensitive"] = sum(int("sensitive_value" in findings) for findings in identifiers)
    result["schema_definition_sensitive"] = sum(int("sensitive_value" in findings) for findings in definitions)
    result["scan_incomplete"] = sum(int("scan_incomplete" in findings) for findings in [*identifiers, *definitions])


def _account_derived(
    conn: sqlite3.Connection,
    name: str,
    result: dict[str, object],
    *,
    view: bool,
) -> None:
    quoted = _quote_identifier(name)
    columns = _table_columns(conn, quoted)
    count = int(conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
    result["derived_columns_accounted"] = int(result["derived_columns_accounted"]) + len(columns)
    result["derived_records"] = int(result["derived_records"]) + count
    key = "derived_views_accounted" if view else "derived_tables_accounted"
    result[key] = int(result[key]) + 1
    column_findings = [_reasons(column[0], "schema_identifier") for column in columns]
    result["schema_identifier_sensitive"] = int(result["schema_identifier_sensitive"]) + sum(
        int("sensitive_value" in findings) for findings in column_findings
    )
    result["scan_incomplete"] = int(result["scan_incomplete"]) + sum(
        int("scan_incomplete" in findings) for findings in column_findings
    )


def _shadow_tables(
    conn: sqlite3.Connection,
    table_names: set[str],
    fts_sql: Mapping[str, str],
) -> set[str]:
    try:
        rows = list(conn.execute("PRAGMA table_list"))
    except sqlite3.Error as exc:
        raise _TableListUnavailable from exc
    if not rows:
        shadows: set[str] = set()
        for name, sql in fts_sql.items():
            suffixes = {"data", "idx", "docsize", "config"}
            if not _fts_has_content_option(sql):
                suffixes.add("content")
            shadows.update(f"{name}_{suffix}" for suffix in suffixes if f"{name}_{suffix}" in table_names)
        return shadows
    kinds = {str(row[1]): str(row[2]) for row in rows if str(row[0]) == "main"}
    if not table_names.issubset(kinds):
        raise sqlite3.OperationalError("shadow metadata unavailable")
    shadows = {name for name, kind in kinds.items() if kind == "shadow"}
    for name, sql in fts_sql.items():
        if _fts_has_content_option(sql):
            shadows.discard(f"{name}_content")
    return shadows


def _scan_authoritative(
    conn: sqlite3.Connection,
    table: str,
    result: dict[str, object],
    surfaces: dict[str, dict[str, object]],
) -> None:
    quoted = _quote_identifier(table)
    columns = [(name, declared) for name, declared, hidden in _table_columns(conn, quoted) if hidden != 1]
    result["tables_accounted"] = int(result["tables_accounted"]) + 1
    result["columns_accounted"] = int(result["columns_accounted"]) + len(columns)
    column_findings = [_reasons(name, "schema_identifier") for name, _ in columns]
    result["schema_identifier_sensitive"] = int(result["schema_identifier_sensitive"]) + sum(
        int("sensitive_value" in findings) for findings in column_findings
    )
    result["scan_incomplete"] = int(result["scan_incomplete"]) + sum(
        int("scan_incomplete" in findings) for findings in column_findings
    )
    for name, declared in columns:
        _ensure_surface(surfaces, _surface(table, name, declared, None))
    projection = ", ".join(_quote_identifier(name) for name, _ in columns)
    for row in conn.execute(f"SELECT {projection} FROM {quoted}"):
        result["records_scanned"] = int(result["records_scanned"]) + 1
        row_flagged = False
        for (column, declared), value in zip(columns, row):
            reasons = _reasons(value, column)
            _record(surfaces, _surface(table, column, declared, value), reasons)
            row_flagged = row_flagged or bool(reasons)
            result["scan_incomplete"] = int(result["scan_incomplete"]) + int("scan_incomplete" in reasons)
        result["records_flagged"] = int(result["records_flagged"]) + int(row_flagged)


def _sqlite_inventory(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"reason": "sqlite_not_available", "status": "BLOCKED"}
    conn: sqlite3.Connection | None = None
    try:
        conn = connect_ro(path)
        conn.execute("BEGIN")
        master = conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        rows = [(str(kind), str(name), sql) for kind, name, sql in master]
        result = _empty_sqlite_result()
        surfaces: dict[str, dict[str, object]] = {}
        _schema_evidence(rows, result)
        tables = [(name, sql) for kind, name, sql in rows if kind == "table"]
        views = [name for kind, name, _ in rows if kind == "view"]
        all_virtual = {name for name, sql in tables if sql and "VIRTUAL TABLE" in sql.upper()}
        fts_sql = {name: sql for name, sql in tables if name in all_virtual and sql and _is_fts5_sql(sql)}
        fts = set(fts_sql)
        shadows = _shadow_tables(
            conn,
            {name for name, _ in tables},
            fts_sql,
        )
        for name, sql in tables:
            if name in shadows or (name in fts and sql and _fts_has_content_option(sql)):
                _account_derived(conn, name, result, view=False)
            else:
                _scan_authoritative(conn, name, result, surfaces)
        for name in views:
            _account_derived(conn, name, result, view=True)
        result["surfaces"] = _freeze_surfaces(surfaces)
        if int(result["scan_incomplete"]):
            result.update({"reason": "sqlite_value_unscannable", "status": "BLOCKED"})
        return result
    except _TableListUnavailable:
        return {"reason": "sqlite_table_list_unavailable", "status": "BLOCKED"}
    except (OSError, sqlite3.Error):
        return {"reason": "sqlite_unavailable", "status": "BLOCKED"}
    finally:
        if conn is not None:
            conn.close()


def _linklike(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        junction = getattr(path, "is_junction", None)
        if junction and junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & _REPARSE_POINT)
    except OSError:
        return True


def _empty_file_result() -> dict[str, object]:
    return {
        "entries_accounted": 0,
        "entry_limit_reached": 0,
        "files_scanned": 0,
        "files_unscannable": 0,
        "files_unavailable": 0,
        "files_refused": 0,
        "reason": "completed",
        "sensitive_files": 0,
        "status": "COMPLETED",
        "truncated_files": 0,
        "surfaces": {},
    }


def _valid_file_bounds(chunk_size: int, max_file_bytes: int, max_entries: int) -> bool:
    return (
        isinstance(chunk_size, int)
        and not isinstance(chunk_size, bool)
        and 1 <= chunk_size <= _MAX_CHUNK_BYTES
        and isinstance(max_file_bytes, int)
        and not isinstance(max_file_bytes, bool)
        and 1 <= max_file_bytes <= _MAX_FILE_BYTES
        and isinstance(max_entries, int)
        and not isinstance(max_entries, bool)
        and 1 <= max_entries <= _MAX_ENTRIES
    )


def _stat_fingerprint(stat_result: object) -> tuple[object, ...]:
    return (
        int(getattr(stat_result, "st_size")),
        getattr(stat_result, "st_mtime_ns", None),
        getattr(stat_result, "st_ctime_ns", None),
        getattr(stat_result, "st_dev", None),
        getattr(stat_result, "st_ino", None),
    )


def _open_file_fingerprint(handle: object, path: Path) -> tuple[object, ...]:
    try:
        fileno = getattr(handle, "fileno")()
        return _stat_fingerprint(os.fstat(fileno))
    except (AttributeError, OSError, TypeError):
        return _stat_fingerprint(path.stat())


def _cross_source_identity(fingerprint: tuple[object, ...]) -> tuple[object, ...]:
    size, modified, _changed, device, inode = fingerprint
    return size, modified, device, inode


def _file_changed(
    path_start: tuple[object, ...],
    path_end: tuple[object, ...],
    open_start: tuple[object, ...],
    open_end: tuple[object, ...],
) -> bool:
    return (
        open_start != open_end
        or path_start != path_end
        or _cross_source_identity(path_start) != _cross_source_identity(open_start)
        or _cross_source_identity(path_end) != _cross_source_identity(open_end)
    )


def _file_surface(path: Path, undecodable: bool) -> str:
    if undecodable:
        return "file.binary"
    suffix = path.suffix.lower()
    if suffix in {".json", ".jsonl", ".ndjson"}:
        return "file.json"
    if suffix in {".txt", ".md", ".log", ".csv"}:
        return "file.text"
    return "file.other"


class _FileIdentityChanged(ValueError):
    pass


def _verified_open_fingerprint(
    handle: object,
    path: Path,
    root: Path,
    path_start: tuple[object, ...],
) -> tuple[object, ...]:
    opened = _open_file_fingerprint(handle, path)
    path.resolve(strict=True).relative_to(root)
    path_open = _stat_fingerprint(path.stat())
    if path_start != path_open or _cross_source_identity(path_open) != _cross_source_identity(opened):
        raise _FileIdentityChanged
    return opened


def _read_file_snapshot(
    handle: object,
    size: int,
    *,
    chunk_size: int,
    max_file_bytes: int,
) -> tuple[bytes, bool]:
    scan_limit = min(size, max_file_bytes, _MAX_SCAN_TEXT_BYTES + 1)
    content = bytearray()
    while len(content) < scan_limit:
        chunk = getattr(handle, "read")(min(chunk_size, scan_limit - len(content)))
        if not chunk:
            break
        content.extend(chunk)
    incomplete = len(content) < min(size, max_file_bytes) or size > _MAX_SCAN_TEXT_BYTES
    return bytes(content), incomplete


def _decode_file(content: bytes) -> tuple[str, bool]:
    try:
        return content.decode("utf-8"), False
    except UnicodeDecodeError:
        return content.decode("utf-8", errors="ignore"), True


def _scan_file(
    path: Path,
    root: Path,
    *,
    chunk_size: int,
    max_file_bytes: int,
) -> tuple[str, set[str], set[str], bool]:
    path_start_fingerprint = _stat_fingerprint(path.stat())
    with path.open("rb") as handle:
        start_fingerprint = _verified_open_fingerprint(handle, path, root, path_start_fingerprint)
        size = int(start_fingerprint[0])
        content, incomplete = _read_file_snapshot(
            handle,
            size,
            chunk_size=chunk_size,
            max_file_bytes=max_file_bytes,
        )
        end_fingerprint = _open_file_fingerprint(handle, path)
    path_end_fingerprint = _stat_fingerprint(path.stat())
    changed = _file_changed(
        path_start_fingerprint,
        path_end_fingerprint,
        start_fingerprint,
        end_fingerprint,
    )
    decoded, undecodable = _decode_file(content)
    reasons = _reasons(decoded, "content")
    if changed or incomplete:
        reasons.add("scan_incomplete")
    if undecodable:
        reasons.add("binary_opaque")
    relative = " ".join(path.relative_to(root).parts)
    return (
        _file_surface(path, undecodable),
        reasons,
        _reasons(relative, "metadata"),
        size > max_file_bytes or incomplete or changed,
    )


def _account_file(
    candidate: Path,
    root: Path,
    result: dict[str, object],
    surfaces: dict[str, dict[str, object]],
    *,
    chunk_size: int,
    max_file_bytes: int,
) -> None:
    if _linklike(candidate):
        result["files_refused"] = int(result["files_refused"]) + 1
        return
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        surface, reasons, metadata, truncated = _scan_file(
            resolved,
            root,
            chunk_size=chunk_size,
            max_file_bytes=max_file_bytes,
        )
        _record(surfaces, surface, reasons)
        _record(surfaces, "file.metadata", metadata)
        result["files_scanned"] = int(result["files_scanned"]) + 1
        result["files_unscannable"] = int(result["files_unscannable"]) + int(
            "scan_incomplete" in reasons or "scan_incomplete" in metadata
        )
        result["sensitive_files"] = int(result["sensitive_files"]) + int(bool(reasons or metadata))
        result["truncated_files"] = int(result["truncated_files"]) + int(truncated)
    except _FileIdentityChanged:
        result["files_unscannable"] = int(result["files_unscannable"]) + 1
    except ValueError:
        result["files_refused"] = int(result["files_refused"]) + 1
    except OSError:
        result["files_unavailable"] = int(result["files_unavailable"]) + 1


def _walk_file_root(
    root: Path,
    result: dict[str, object],
    surfaces: dict[str, dict[str, object]],
    *,
    chunk_size: int,
    max_file_bytes: int,
    max_entries: int,
) -> bool:
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                iterator = iter(entries)
                while int(result["entries_accounted"]) < max_entries:
                    try:
                        entry = next(iterator)
                    except StopIteration:
                        break
                    result["entries_accounted"] = int(result["entries_accounted"]) + 1
                    candidate = Path(entry.path)
                    if _linklike(candidate):
                        result["files_refused"] = int(result["files_refused"]) + 1
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(candidate)
                        elif entry.is_file(follow_symlinks=False):
                            _account_file(
                                candidate,
                                root,
                                result,
                                surfaces,
                                chunk_size=chunk_size,
                                max_file_bytes=max_file_bytes,
                            )
                        else:
                            result["files_refused"] = int(result["files_refused"]) + 1
                    except OSError:
                        result["files_unavailable"] = int(result["files_unavailable"]) + 1
                if int(result["entries_accounted"]) >= max_entries:
                    return False
        except OSError:
            result["files_unavailable"] = int(result["files_unavailable"]) + 1
    return True


def _entry_limit_result(max_entries: int) -> dict[str, object]:
    result = _empty_file_result()
    result.update(
        {
            "entries_accounted": max_entries,
            "entry_limit_reached": 1,
            "reason": "entry_limit",
            "status": "BLOCKED",
        }
    )
    return result


def _finalize_file_result(
    result: dict[str, object],
    surfaces: dict[str, dict[str, object]],
    *,
    root_refused: bool,
) -> dict[str, object]:
    result["surfaces"] = _freeze_surfaces(surfaces)
    result["symlink_refused"] = result["files_refused"]
    if root_refused:
        result.update({"reason": "root_refused", "status": "BLOCKED"})
    elif int(result["files_unavailable"]):
        result.update({"reason": "file_unavailable", "status": "BLOCKED"})
    elif int(result["files_unscannable"]):
        result.update({"reason": "file_unscannable", "status": "BLOCKED"})
    elif int(result["truncated_files"]):
        result.update({"reason": "file_truncated", "status": "BLOCKED"})
    return result


def _file_inventory(
    roots: Iterable[Path],
    *,
    chunk_size: int,
    max_file_bytes: int,
    max_entries: int,
) -> dict[str, object]:
    requested = list(roots)
    result = _empty_file_result()
    surfaces: dict[str, dict[str, object]] = {}
    if not _valid_file_bounds(chunk_size, max_file_bytes, max_entries):
        result.update({"reason": "invalid_file_bounds", "status": "BLOCKED"})
        return result
    if not requested:
        result.update({"reason": "roots_not_provided", "status": "BLOCKED"})
        return result
    root_refused = False
    for supplied in requested:
        try:
            root = supplied.resolve(strict=True)
        except OSError:
            result["files_unavailable"] = int(result["files_unavailable"]) + 1
            continue
        if not root.is_dir() or _linklike(supplied):
            result["files_refused"] = int(result["files_refused"]) + 1
            root_refused = True
            continue
        complete = _walk_file_root(
            root,
            result,
            surfaces,
            chunk_size=chunk_size,
            max_file_bytes=max_file_bytes,
            max_entries=max_entries,
        )
        if not complete:
            return _entry_limit_result(max_entries)
    return _finalize_file_result(result, surfaces, root_refused=root_refused)


def _qdrant_block(
    reason: str,
    total: int = 0,
    surfaces: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "reason": reason,
        "status": "BLOCKED-EXTERNAL",
    }
    if total or surfaces:
        result["payloads_scanned"] = total
        result["surfaces"] = _freeze_surfaces(surfaces or {})
    return result


def _offset_marker(offset: object) -> str | None:
    if not _offset_shape_bounded(offset):
        return None
    try:
        encoded = json.dumps(
            offset,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError):
        return None
    if len(encoded) > _MAX_OFFSET_BYTES:
        return None
    return sha256(encoded).hexdigest()


def _offset_shape_bounded(offset: object) -> bool:
    pending: list[tuple[object, int]] = [(offset, 0)]
    containers: set[int] = set()
    nodes = 0
    text_bytes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if nodes > _MAX_OFFSET_NODES or depth > _MAX_OFFSET_DEPTH:
            return False
        if value is None or type(value) is bool:
            continue
        if type(value) is str:
            if len(value) > _MAX_OFFSET_BYTES:
                return False
            try:
                text_bytes += len(value.encode("utf-8"))
            except UnicodeEncodeError:
                return False
            if text_bytes > _MAX_OFFSET_BYTES:
                return False
            continue
        if type(value) is int:
            if value.bit_length() > _MAX_OFFSET_BYTES * 8:
                return False
            continue
        if type(value) is float:
            if not isfinite(value):
                return False
            continue
        if type(value) not in {dict, list}:
            return False
        identity = id(value)
        if identity in containers:
            return False
        containers.add(identity)
        if len(pending) + len(value) > _MAX_OFFSET_NODES:
            return False
        if type(value) is list:
            pending.extend((item, depth + 1) for item in value)
            continue
        for key, nested in value.items():
            if type(key) is not str:
                return False
            pending.append((key, depth + 1))
            pending.append((nested, depth + 1))
    return True


def _qdrant_payload_reasons(payload: object) -> set[str]:
    if type(payload) is not dict:
        return {"scan_incomplete"}
    pending: list[tuple[object, int]] = [(payload, 0)]
    containers: set[int] = set()
    nodes = 0
    text_bytes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if nodes > _MAX_SCAN_NODES or depth > _MAX_SCAN_DEPTH:
            return {"scan_incomplete"}
        if value is None or type(value) is bool:
            continue
        if type(value) is bytes:
            return {"binary_opaque"}
        if type(value) is str:
            try:
                text_bytes += len(value.encode("utf-8"))
            except UnicodeEncodeError:
                return {"scan_incomplete"}
            if text_bytes > _MAX_SCAN_TEXT_BYTES:
                return {"scan_incomplete"}
            continue
        if type(value) is int:
            if value.bit_length() > _MAX_SCAN_TEXT_BYTES * 8:
                return {"scan_incomplete"}
            continue
        if type(value) is float:
            if not isfinite(value):
                return {"scan_incomplete"}
            continue
        if type(value) not in {dict, list} or id(value) in containers:
            return {"scan_incomplete"}
        containers.add(id(value))
        if len(pending) + len(value) > _MAX_SCAN_NODES:
            return {"scan_incomplete"}
        if type(value) is dict:
            for key, nested in value.items():
                if type(key) is not str:
                    return {"scan_incomplete"}
                pending.append((key, depth + 1))
                pending.append((nested, depth + 1))
        else:
            pending.extend((item, depth + 1) for item in value)
    return _reasons(payload, "payload")


def _valid_qdrant_bounds(limit: object, max_pages: object) -> bool:
    return (
        isinstance(limit, int)
        and not isinstance(limit, bool)
        and isinstance(max_pages, int)
        and not isinstance(max_pages, bool)
        and 1 <= limit <= 1000
        and 1 <= max_pages <= 10000
    )


def scan_qdrant_payloads(
    page: Callable[..., Mapping[str, object]],
    *,
    limit: int = 100,
    max_pages: int = 1000,
) -> dict[str, object]:
    if not _valid_qdrant_bounds(limit, max_pages):
        return {"reason": "qdrant_invalid_bounds", "status": "BLOCKED-EXTERNAL"}
    surfaces: dict[str, dict[str, object]] = {}
    offset: object = None
    seen: set[str] = set()
    total = 0
    for _ in range(max_pages):
        try:
            response = page(limit=limit, with_payload=True, with_vector=False, offset=offset)
        except Exception:
            return _qdrant_block("qdrant_transport_error", total, surfaces)
        envelope = response.get("result") if type(response) is dict else None
        if type(envelope) is not dict or type(envelope.get("points")) is not list or "next_page_offset" not in envelope:
            return _qdrant_block("qdrant_malformed_response", total, surfaces)
        points = envelope["points"]
        if len(points) > limit:
            return _qdrant_block("qdrant_page_oversized", total, surfaces)
        for point in points:
            if type(point) is not dict:
                return _qdrant_block("qdrant_malformed_point", total, surfaces)
            if "payload" not in point:
                return _qdrant_block("qdrant_payload_missing", total, surfaces)
            total += 1
            reasons = _qdrant_payload_reasons(point["payload"])
            _record(surfaces, "qdrant.payload", reasons)
            if reasons & {"scan_incomplete", "binary_opaque"}:
                return _qdrant_block("qdrant_payload_unscannable", total, surfaces)
        offset = envelope.get("next_page_offset")
        if offset is None:
            return {"payloads_scanned": total, "status": "COMPLETED", "surfaces": _freeze_surfaces(surfaces)}
        marker = _offset_marker(offset)
        if marker is None:
            return _qdrant_block("qdrant_malformed_offset", total, surfaces)
        if marker in seen:
            return _qdrant_block("qdrant_repeated_offset", total, surfaces)
        seen.add(marker)
    return _qdrant_block("qdrant_page_limit", total, surfaces)


def run_inventory(
    db_path: str | Path | None,
    *,
    artifact_roots: Iterable[str | Path] = (),
    spool_roots: Iterable[str | Path] = (),
    qdrant_page: Callable[..., Mapping[str, object]] | None = None,
    chunk_size: int = 65536,
    max_file_bytes: int = 1048576,
    max_entries: int = 100000,
) -> dict[str, object]:
    """Perform a deterministic dry-run without returning raw schema or values."""
    sqlite = (
        _sqlite_inventory(Path(db_path))
        if db_path is not None
        else {"reason": "sqlite_not_configured", "status": "BLOCKED"}
    )
    qdrant = (
        scan_qdrant_payloads(qdrant_page)
        if qdrant_page
        else {"reason": "qdrant_not_configured", "status": "BLOCKED-EXTERNAL"}
    )
    artifacts = _file_inventory(
        (Path(item) for item in artifact_roots),
        chunk_size=chunk_size,
        max_file_bytes=max_file_bytes,
        max_entries=max_entries,
    )
    spool = _file_inventory(
        (Path(item) for item in spool_roots),
        chunk_size=chunk_size,
        max_file_bytes=max_file_bytes,
        max_entries=max_entries,
    )
    local_blocked = (
        sqlite.get("status") == "BLOCKED" or artifacts["status"] == "BLOCKED" or spool["status"] == "BLOCKED"
    )
    status = "BLOCKED" if local_blocked else qdrant["status"]
    return {
        "artifacts": artifacts,
        "classification": "LEGACY-SENSITIVITY-INVENTORY",
        "mode": "dry_run",
        "qdrant": qdrant,
        "recommendation": "REVIEW_ONLY",
        "spool": spool,
        "sqlite": sqlite,
        "status": status,
    }
