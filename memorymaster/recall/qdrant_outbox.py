"""Bounded metadata-only outbox for replayable Qdrant maintenance writes."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import threading

from memorymaster.core.security import is_sensitive_claim


ENV_OUTBOX_DIR = "MEMORYMASTER_QDRANT_OUTBOX_DIR"
DEFAULT_MAX_ENTRIES = 10_000
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_LOCK = threading.Lock()


def _path(db_path: str | Path) -> Path:
    source = Path(db_path)
    resolved = str(source.resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
    root = Path(os.environ.get(ENV_OUTBOX_DIR) or Path.home() / ".memorymaster" / "qdrant-outbox")
    return root / f"{source.name}-{digest}.jsonl"


def _valid(entry: object) -> bool:
    if not isinstance(entry, dict) or set(entry) != {"op", "claim_id", "content_hash"}:
        return False
    claim_id = entry.get("claim_id")
    operation = entry.get("op")
    content_hash = entry.get("content_hash")
    if isinstance(claim_id, bool) or not isinstance(claim_id, int) or claim_id <= 0:
        return False
    if operation == "delete":
        return content_hash is None
    return operation == "upsert" and isinstance(content_hash, str) and _HASH_RE.fullmatch(content_hash) is not None


def pending(db_path: str | Path) -> list[dict[str, object]]:
    path = _path(db_path)
    if not path.exists():
        return []
    entries: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except (TypeError, ValueError):
            continue
        if _valid(entry):
            entries.append(entry)
    return entries


def enqueue(
    db_path: str | Path,
    operation: str,
    claim_id: int,
    content_hash: str | None,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> bool:
    """Append one operation, returning False when validation/bounds reject it."""
    entry = {"op": operation, "claim_id": claim_id, "content_hash": content_hash}
    if not _valid(entry) or max_entries <= 0 or max_bytes <= 0:
        return False
    line = json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n"
    path = _path(db_path)
    with _LOCK:
        existing = pending(db_path)
        size = path.stat().st_size if path.exists() else 0
        if len(existing) >= max_entries or size + len(line.encode("utf-8")) > max_bytes:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    return True


def _apply(entry: dict[str, object], store, backend) -> bool:
    claim_id = int(entry["claim_id"])
    if entry["op"] == "delete":
        return bool(backend.delete_claim(claim_id))
    claim = store.get_claim(claim_id, include_citations=True)
    if claim is None or claim.status == "archived" or is_sensitive_claim(claim):
        return bool(backend.delete_claim(claim_id))
    return bool(backend.upsert_claim(claim))


def replay(
    db_path: str | Path,
    store,
    backend,
    *,
    max_operations: int = 500,
) -> dict[str, int]:
    """Replay a bounded prefix and atomically retain failed/unattempted work."""
    if max_operations <= 0:
        raise ValueError("max_operations must be positive")
    with _LOCK:
        entries = pending(db_path)
        attempted = min(len(entries), max_operations)
        retained: list[dict[str, object]] = []
        completed = 0
        for index, entry in enumerate(entries):
            try:
                applied = index < attempted and _apply(entry, store, backend)
            except Exception:
                applied = False
            if not applied:
                retained.append(entry)
            else:
                completed += 1
        path = _path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        payload = "".join(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n" for item in retained)
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, path)
    return {"attempted": attempted, "completed": completed, "remaining": len(retained)}
