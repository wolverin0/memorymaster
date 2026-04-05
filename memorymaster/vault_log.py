"""Vault log — append-only chronological record of all knowledge base operations.

Implements Karpathy's log.md: parseable, chronological, never edited — only appended.

Format:
    ## [2026-04-04T14:30:00Z] ingest | subject: auth | claim #7890
    ## [2026-04-04T14:35:00Z] query | "how does auth work" | 5 results
    ## [2026-04-04T15:00:00Z] lint | 3 contradictions, 5 orphans
    ## [2026-04-04T15:10:00Z] curate | 91 claims → 11 topics
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_VAULT = None


def _get_log_path(vault_dir: str | Path | None = None) -> Path:
    """Get path to log.md in the vault directory."""
    if vault_dir:
        return Path(vault_dir) / "log.md"
    # Fallback: try to find the vault from env or default
    import os
    default = os.environ.get("MEMORYMASTER_VAULT_DIR", "")
    if default:
        return Path(default) / "log.md"
    return Path("obsidian-vault") / "log.md"


def _ensure_log(path: Path) -> None:
    """Create log.md with header if it doesn't exist."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# MemoryMaster Knowledge Log\n\n"
            "Append-only chronological record of all knowledge base operations.\n\n"
            "---\n\n",
            encoding="utf-8",
        )


def append_log(
    operation: str,
    details: str,
    vault_dir: str | Path | None = None,
) -> None:
    """Append an entry to log.md.

    Args:
        operation: ingest, query, lint, curate, steward, dream-sync, etc.
        details: one-line description of what happened
        vault_dir: path to the vault directory
    """
    try:
        path = _get_log_path(vault_dir)
        _ensure_log(path)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = f"## [{now}] {operation} | {details}\n\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception as e:
        logger.debug("Failed to append log: %s", e)


def log_ingest(claim_id: int, subject: str | None, scope: str, vault_dir: str | Path | None = None) -> None:
    """Log a claim ingest event."""
    subj = subject or "unknown"
    append_log("ingest", f"claim #{claim_id} | subject: {subj} | scope: {scope}", vault_dir)


def log_query(query_text: str, result_count: int, vault_dir: str | Path | None = None) -> None:
    """Log a query event."""
    q = query_text[:60].replace("\n", " ")
    append_log("query", f'"{q}" | {result_count} results', vault_dir)


def log_lint(report: dict, vault_dir: str | Path | None = None) -> None:
    """Log a lint event."""
    c = len(report.get("contradictions", []))
    o = len(report.get("orphans", []))
    g = len(report.get("gaps", []))
    s = len(report.get("stale", []))
    append_log("lint", f"{c} contradictions, {o} orphans, {g} gaps, {s} stale", vault_dir)


def log_curate(stats: dict, vault_dir: str | Path | None = None) -> None:
    """Log a curate event."""
    claims = stats.get("claims", 0)
    topics = stats.get("topics", 0)
    files = stats.get("files_written", 0)
    append_log("curate", f"{claims} claims -> {topics} topics, {files} files", vault_dir)


def log_steward(result: dict, vault_dir: str | Path | None = None) -> None:
    """Log a steward cycle event."""
    v = result.get("validator", {})
    confirmed = v.get("confirmed", 0)
    pending = v.get("pending", 0)
    append_log("steward", f"confirmed={confirmed}, pending={pending}", vault_dir)


def log_sync(direction: str, merged: int, vault_dir: str | Path | None = None) -> None:
    """Log a sync event."""
    append_log("sync", f"{direction} | {merged} claims merged", vault_dir)
