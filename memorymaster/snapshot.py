"""Git-backed DB versioning: snapshot, list, rollback, diff."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


SNAPSHOTS_DIR_NAME = "snapshots"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _utc_timestamp() -> str:
    """Compact UTC timestamp for filenames: YYYYMMDD_HHMMSS_ffffff."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def get_git_head(workspace_root: Path) -> str | None:
    """Return the current git HEAD commit hash, or None."""
    resolved = workspace_root.resolve()
    try:
        proc = subprocess.run(
            ["git", "-C", str(resolved), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    head = proc.stdout.strip()
    if not head or len(head) != 40 or not all(c in "0123456789abcdef" for c in head):
        return None
    return head


def _snapshots_dir(db_path: Path) -> Path:
    """Return the snapshots directory next to the DB file.

    Layout: <db_parent>/.memorymaster/snapshots/
    """
    return db_path.parent / ".memorymaster" / SNAPSHOTS_DIR_NAME


@dataclass
class SnapshotInfo:
    snapshot_id: str
    filename: str
    path: str
    commit_hash: str | None
    timestamp: str
    message: str
    size_bytes: int


@dataclass
class SnapshotDiff:
    snapshot_id: str
    added: list[dict] = field(default_factory=list)
    removed: list[dict] = field(default_factory=list)
    changed: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


def create_snapshot(
    db_path: Path,
    workspace_root: Path | None = None,
    *,
    message: str = "",
) -> SnapshotInfo:
    """Create a versioned snapshot of the DB using SQLite backup API."""
    db_path = db_path.resolve()
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    snap_dir = _snapshots_dir(db_path)
    snap_dir.mkdir(parents=True, exist_ok=True)

    commit_hash = get_git_head(workspace_root or db_path.parent)
    ts = _utc_timestamp()
    short_hash = commit_hash[:8] if commit_hash else "nogit"
    filename = f"{short_hash}_{ts}.db"
    snap_path = snap_dir / filename

    # Use SQLite backup API for a consistent copy
    src_conn = sqlite3.connect(str(db_path))
    try:
        dst_conn = sqlite3.connect(str(snap_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    # Write metadata sidecar
    meta_path = snap_path.with_suffix(".meta")
    meta_lines = [
        f"commit={commit_hash or ''}",
        f"timestamp={_utc_now()}",
        f"message={message}",
        f"source_db={str(db_path)}",
    ]
    meta_path.write_text("\n".join(meta_lines), encoding="utf-8")

    snapshot_id = snap_path.stem
    return SnapshotInfo(
        snapshot_id=snapshot_id,
        filename=filename,
        path=str(snap_path),
        commit_hash=commit_hash,
        timestamp=_utc_now(),
        message=message,
        size_bytes=snap_path.stat().st_size,
    )


def _parse_meta(meta_path: Path) -> dict[str, str]:
    """Parse a .meta sidecar file into a dict."""
    result: dict[str, str] = {}
    if not meta_path.exists():
        return result
    for line in meta_path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def list_snapshots(db_path: Path) -> list[SnapshotInfo]:
    """List all snapshots sorted by filename (newest first)."""
    db_path = db_path.resolve()
    snap_dir = _snapshots_dir(db_path)
    if not snap_dir.exists():
        return []

    snapshots: list[SnapshotInfo] = []
    for snap_file in sorted(snap_dir.glob("*.db"), reverse=True):
        meta = _parse_meta(snap_file.with_suffix(".meta"))
        commit_hash = meta.get("commit", "") or None
        snapshots.append(
            SnapshotInfo(
                snapshot_id=snap_file.stem,
                filename=snap_file.name,
                path=str(snap_file),
                commit_hash=commit_hash,
                timestamp=meta.get("timestamp", ""),
                message=meta.get("message", ""),
                size_bytes=snap_file.stat().st_size,
            )
        )
    return snapshots


def _resolve_snapshot_path(db_path: Path, snapshot_id: str) -> Path:
    """Find a snapshot file by its ID (stem of the .db file)."""
    db_path = db_path.resolve()
    snap_dir = _snapshots_dir(db_path)
    # Try exact match first
    exact = snap_dir / f"{snapshot_id}.db"
    if exact.exists():
        return exact
    # Try prefix match
    matches = [f for f in snap_dir.glob("*.db") if f.stem.startswith(snapshot_id)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(m.stem for m in matches)
        raise ValueError(f"Ambiguous snapshot_id '{snapshot_id}', matches: {names}")
    raise FileNotFoundError(f"Snapshot not found: {snapshot_id}")


def rollback(db_path: Path, snapshot_id: str) -> SnapshotInfo:
    """Restore the DB from a snapshot. Creates a pre-rollback snapshot first."""
    db_path = db_path.resolve()
    snap_path = _resolve_snapshot_path(db_path, snapshot_id)

    # Safety: create a pre-rollback snapshot
    create_snapshot(db_path, message=f"pre-rollback (restoring {snapshot_id})")

    # Use SQLite backup API to restore
    src_conn = sqlite3.connect(str(snap_path))
    try:
        dst_conn = sqlite3.connect(str(db_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    meta = _parse_meta(snap_path.with_suffix(".meta"))
    return SnapshotInfo(
        snapshot_id=snap_path.stem,
        filename=snap_path.name,
        path=str(snap_path),
        commit_hash=meta.get("commit", "") or None,
        timestamp=meta.get("timestamp", ""),
        message=meta.get("message", ""),
        size_bytes=snap_path.stat().st_size,
    )


def _extract_claims(conn: sqlite3.Connection) -> dict[int, dict]:
    """Extract all claims from a connection keyed by id."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, text, status, confidence, pinned, claim_type, subject, "
        "predicate, object_value, scope, volatility, updated_at "
        "FROM claims ORDER BY id"
    ).fetchall()
    return {
        row["id"]: dict(row)
        for row in rows
    }


def diff_snapshot(db_path: Path, snapshot_id: str) -> SnapshotDiff:
    """Compare current DB state against a snapshot."""
    db_path = db_path.resolve()
    snap_path = _resolve_snapshot_path(db_path, snapshot_id)

    current_conn = sqlite3.connect(str(db_path))
    snap_conn = sqlite3.connect(str(snap_path))
    try:
        current_claims = _extract_claims(current_conn)
        snap_claims = _extract_claims(snap_conn)
    finally:
        current_conn.close()
        snap_conn.close()

    current_ids = set(current_claims.keys())
    snap_ids = set(snap_claims.keys())

    added_ids = current_ids - snap_ids
    removed_ids = snap_ids - current_ids
    common_ids = current_ids & snap_ids

    added = [
        {"id": cid, "text": current_claims[cid]["text"], "status": current_claims[cid]["status"]}
        for cid in sorted(added_ids)
    ]
    removed = [
        {"id": cid, "text": snap_claims[cid]["text"], "status": snap_claims[cid]["status"]}
        for cid in sorted(removed_ids)
    ]

    # Compare fields that matter
    _compare_fields = ("text", "status", "confidence", "pinned", "claim_type",
                       "subject", "predicate", "object_value", "scope", "volatility")
    changed: list[dict] = []
    for cid in sorted(common_ids):
        cur = current_claims[cid]
        old = snap_claims[cid]
        diffs = {}
        for fld in _compare_fields:
            if cur.get(fld) != old.get(fld):
                diffs[fld] = {"old": old.get(fld), "new": cur.get(fld)}
        if diffs:
            changed.append({"id": cid, "text": cur["text"], "changes": diffs})

    return SnapshotDiff(
        snapshot_id=snapshot_id,
        added=added,
        removed=removed,
        changed=changed,
        summary={
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "unchanged": len(common_ids) - len(changed),
        },
    )


# ---------------------------------------------------------------------------
# Git hook installer
# ---------------------------------------------------------------------------

_POST_COMMIT_HOOK = """\
#!/bin/sh
# memorymaster: auto-snapshot claim DB after each commit
# Installed by: memorymaster install-hook

DB_PATH="${MEMORYMASTER_DB:-memorymaster.db}"
if [ -f "$DB_PATH" ]; then
    python -m memorymaster --db "$DB_PATH" snapshot --message "auto: post-commit $(git rev-parse --short HEAD)" 2>/dev/null || true
fi
"""


def install_git_hook(workspace_root: Path) -> dict:
    """Install a post-commit hook that auto-snapshots the DB."""
    workspace_root = workspace_root.resolve()
    git_dir = workspace_root / ".git"
    if not git_dir.is_dir():
        raise FileNotFoundError(f"Not a git repository: {workspace_root}")

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "post-commit"

    already_exists = hook_path.exists()
    appended = False

    if already_exists:
        existing = hook_path.read_text(encoding="utf-8")
        if "memorymaster" in existing:
            return {
                "installed": False,
                "reason": "hook already contains memorymaster snippet",
                "path": str(hook_path),
            }
        # Append to existing hook
        hook_path.write_text(
            existing.rstrip() + "\n\n" + _POST_COMMIT_HOOK,
            encoding="utf-8",
        )
        appended = True
    else:
        hook_path.write_text(_POST_COMMIT_HOOK, encoding="utf-8")

    # Make executable on Unix
    try:
        hook_path.chmod(0o755)
    except OSError:
        pass

    return {
        "installed": True,
        "appended": appended,
        "path": str(hook_path),
    }
