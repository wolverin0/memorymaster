"""Staleness detection for claims whose cited source files have changed.

Supports two detection modes:
- **mtime**: Compare file modification time against claim's last_validated_at.
- **git**: Use ``git diff`` to detect changes since last validation timestamp.

When a cited source file is found to have changed after the claim was last
validated, the claim is transitioned to ``stale`` status.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from memorymaster.lifecycle import can_transition, transition_claim
from memorymaster.models import Claim


@dataclass(frozen=True)
class StalenessResult:
    """Summary of a staleness check run."""

    scanned: int = 0
    stale_detected: int = 0
    already_stale: int = 0
    skipped_no_citations: int = 0
    skipped_pinned: int = 0
    details: list[dict[str, object]] = field(default_factory=list)


def _parse_iso(dt: str) -> datetime:
    return datetime.fromisoformat(dt)


def _file_mtime_utc(path: Path) -> datetime | None:
    """Return file modification time as a UTC datetime, or None if missing."""
    try:
        stat = path.stat()
        return datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    except (OSError, ValueError):
        return None


def _git_file_changed_since(
    filepath: Path,
    since: datetime,
    workspace: Path,
) -> bool:
    """Check if a file has git changes since *since* timestamp.

    Uses ``git log --since`` to detect commits touching the file.
    Returns True if there are commits after *since*, False otherwise.
    Falls back to False on any git error (not a repo, etc.).
    """
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        result = subprocess.run(
            [
                "git", "log", "--oneline", "--since", since_iso,
                "--", str(filepath),
            ],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False
        return bool(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _extract_file_paths(claim: Claim, workspace: Path) -> list[Path]:
    """Extract resolvable file paths from a claim's citations.

    Citation sources that look like file paths (contain a slash or dot-extension)
    are resolved relative to *workspace*. Non-path sources (URLs, plain labels)
    are ignored.
    """
    paths: list[Path] = []
    for citation in claim.citations:
        source = citation.source.strip()
        if not source:
            continue
        # Skip URLs
        if source.startswith(("http://", "https://", "ftp://")):
            continue
        # Heuristic: treat as file path if it contains a separator or extension
        if "/" in source or "\\" in source or "." in source:
            candidate = Path(source)
            if not candidate.is_absolute():
                candidate = workspace / candidate
            paths.append(candidate)
    return paths


def check_claim_staleness(
    claim: Claim,
    workspace: Path,
    *,
    mode: str = "mtime",
) -> tuple[bool, list[str]]:
    """Check whether any cited files have changed since the claim was validated.

    Returns (is_stale, list_of_changed_files).
    """
    if not claim.citations:
        return False, []

    file_paths = _extract_file_paths(claim, workspace)
    if not file_paths:
        return False, []

    # Use last_validated_at if available, otherwise fall back to updated_at
    reference_dt_str = claim.last_validated_at or claim.updated_at
    reference_dt = _parse_iso(reference_dt_str)
    if reference_dt.tzinfo is None:
        reference_dt = reference_dt.replace(tzinfo=timezone.utc)

    changed_files: list[str] = []
    for fpath in file_paths:
        if mode == "git":
            if _git_file_changed_since(fpath, reference_dt, workspace):
                changed_files.append(str(fpath))
        else:
            # mtime mode
            mtime = _file_mtime_utc(fpath)
            if mtime is not None and mtime > reference_dt:
                changed_files.append(str(fpath))

    return bool(changed_files), changed_files


def run(
    store,
    workspace: Path,
    *,
    mode: str = "mtime",
    dry_run: bool = False,
    limit: int = 500,
    statuses: tuple[str, ...] = ("confirmed", "candidate"),
) -> StalenessResult:
    """Scan claims with file-based citations and flag stale ones.

    Parameters
    ----------
    store:
        The SQLiteStore (or compatible) instance.
    workspace:
        Root directory for resolving relative citation paths.
    mode:
        Detection mode: ``"mtime"`` (file modification time) or ``"git"``.
    dry_run:
        If True, detect but do not transition claims.
    limit:
        Maximum number of claims to scan per status.
    statuses:
        Which claim statuses to scan (default: confirmed + candidate).
    """
    scanned = 0
    stale_detected = 0
    already_stale = 0
    skipped_no_citations = 0
    skipped_pinned = 0
    details: list[dict[str, object]] = []

    for status in statuses:
        claims = store.find_by_status(status, limit=limit, include_citations=True)
        for claim in claims:
            scanned += 1

            if claim.pinned:
                skipped_pinned += 1
                continue

            if not claim.citations:
                skipped_no_citations += 1
                continue

            if claim.status == "stale":
                already_stale += 1
                continue

            is_stale, changed_files = check_claim_staleness(
                claim, workspace, mode=mode,
            )

            if not is_stale:
                continue

            stale_detected += 1
            detail: dict[str, object] = {
                "claim_id": claim.id,
                "text": claim.text[:120],
                "changed_files": changed_files,
                "applied": False,
            }

            if not dry_run and can_transition(claim.status, "stale"):
                reason = (
                    f"source file(s) changed ({mode}): "
                    + ", ".join(os.path.basename(f) for f in changed_files[:3])
                )
                transition_claim(
                    store,
                    claim_id=claim.id,
                    to_status="stale",
                    reason=reason,
                    event_type="staleness",
                )
                detail["applied"] = True

            details.append(detail)

    return StalenessResult(
        scanned=scanned,
        stale_detected=stale_detected,
        already_stale=already_stale,
        skipped_no_citations=skipped_no_citations,
        skipped_pinned=skipped_pinned,
        details=details,
    )
