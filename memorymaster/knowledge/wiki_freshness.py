"""Wiki freshness metric — Option A (absorb recency).

Roadmap item 11.8. Computes per-article freshness based on how long it has been
since the article was last absorbed by `wiki-absorb`.

Design notes (Option A, simple variant):
  - Truth source for "last absorb" is the article frontmatter `date:` field,
    which `wiki_engine.py:_write_article` stamps on every absorb run.
  - When the frontmatter `date` is missing or unparseable we fall back to the
    file's mtime so a never-absorbed legacy article still gets a signal.
  - freshness_score = exp(-days_since_last_absorb / 30), clamped to [0, 1].
    At 30 days the score is ~0.37, at 60 days ~0.14, at 90 days ~0.05.

This module is READ-only over the vault — it never mutates wiki article files.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# Freshness half-life (measured via exp(-days/SCALE))
# 30 days matches the composite proposal weights in the Option A spec.
FRESHNESS_SCALE_DAYS = 30.0

# Threshold below which `lint-vault` emits a STALE_ARTICLE warning.
# ~0.2 corresponds to ~48 days since last absorb.
STALE_ARTICLE_THRESHOLD = 0.2


@dataclass(frozen=True)
class ArticleFreshness:
    """Freshness snapshot for one wiki article."""

    path: Path
    title: str
    scope: str
    days_since_absorb: float
    freshness_score: float


def _parse_frontmatter_date(text: str) -> datetime | None:
    """Extract the `date:` field from a wiki frontmatter block.

    Returns None if the file has no frontmatter, no `date:` line, or the value
    cannot be parsed as ISO-8601 / YYYY-MM-DD. Tolerant of quoted values.
    """
    if not text.startswith("---"):
        return None
    # Read up to the closing --- (keep frontmatter scan bounded to ~4KB).
    head = text[:4096]
    end = head.find("\n---", 3)
    if end == -1:
        return None
    block = head[3:end]
    match = re.search(r"^date:\s*([^\n#]+)\s*$", block, re.MULTILINE)
    if not match:
        return None
    raw = match.group(1).strip().strip("'\"")
    if not raw:
        return None
    # Try ISO-8601 first (handles full timestamps), then bare YYYY-MM-DD.
    for parser in (
        lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),
        lambda s: datetime.strptime(s, "%Y-%m-%d"),
    ):
        try:
            dt = parser(raw)
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def _parse_frontmatter_title(text: str) -> str | None:
    """Best-effort title extraction from `title:` frontmatter line."""
    if not text.startswith("---"):
        return None
    head = text[:4096]
    end = head.find("\n---", 3)
    if end == -1:
        return None
    block = head[3:end]
    match = re.search(r"^title:\s*(.+?)\s*$", block, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip().strip("'\"")


def _compute_score(days: float) -> float:
    """Exponential decay with 30-day scale, clamped to [0, 1]."""
    if days < 0:
        days = 0.0
    score = math.exp(-days / FRESHNESS_SCALE_DAYS)
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return score


def _file_mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def freshness_for_article(
    path: Path, *, now: datetime | None = None
) -> ArticleFreshness | None:
    """Compute freshness for a single wiki article.

    Returns None for non-existent or unreadable files.
    The parent directory name is used as the article's scope (mirrors the
    on-disk layout `obsidian-vault/wiki/<scope>/<slug>.md`).
    """
    if not path.exists() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    now = now or datetime.now(timezone.utc)

    absorb_ts = _parse_frontmatter_date(text)
    if absorb_ts is None:
        try:
            absorb_ts = _file_mtime(path)
        except OSError:
            return None

    delta_seconds = (now - absorb_ts).total_seconds()
    days = max(0.0, delta_seconds / 86400.0)
    score = _compute_score(days)

    title = _parse_frontmatter_title(text) or path.stem
    scope = path.parent.name

    return ArticleFreshness(
        path=path,
        title=title,
        scope=scope,
        days_since_absorb=days,
        freshness_score=score,
    )


def scan_vault(
    vault_root: Path | str,
    *,
    now: datetime | None = None,
) -> list[ArticleFreshness]:
    """Scan a vault root and return freshness data for every article.

    `vault_root` may be either the vault root (containing scope dirs) or the
    wiki root directly. Articles under `bases/`, any path starting with `_`, or
    any file not ending in `.md` are ignored. The returned list is sorted from
    stalest (lowest score) to freshest.
    """
    root = Path(vault_root)
    if not root.exists():
        return []
    now = now or datetime.now(timezone.utc)

    results: list[ArticleFreshness] = []
    for md in root.rglob("*.md"):
        # Skip obsidian vault bookkeeping files and generated indexes.
        parts = md.parts
        if any(p.startswith("_") for p in parts[-2:]):
            continue
        if "bases" in parts:
            continue
        snap = freshness_for_article(md, now=now)
        if snap is None:
            continue
        results.append(snap)

    results.sort(key=lambda a: a.freshness_score)
    return results


def bucket_distribution(snapshots: Iterable[ArticleFreshness]) -> dict[str, int]:
    """Count articles per freshness bucket.

    Buckets align with the spec's composite-proposal cut-offs:
      - `fresh`    : freshness_score >= 0.5
      - `mid`      : 0.2 <= score < 0.5
      - `stale`    : score < 0.2
    """
    counts = {"fresh": 0, "mid": 0, "stale": 0}
    for snap in snapshots:
        if snap.freshness_score >= 0.5:
            counts["fresh"] += 1
        elif snap.freshness_score >= STALE_ARTICLE_THRESHOLD:
            counts["mid"] += 1
        else:
            counts["stale"] += 1
    return counts


def as_jsonable(snapshots: Iterable[ArticleFreshness]) -> list[dict]:
    """Render snapshots for `--json` output."""
    out: list[dict] = []
    for snap in snapshots:
        out.append(
            {
                "path": str(snap.path),
                "title": snap.title,
                "scope": snap.scope,
                "days_since_absorb": round(snap.days_since_absorb, 2),
                "freshness_score": round(snap.freshness_score, 4),
            }
        )
    return out


def to_json(snapshots: Iterable[ArticleFreshness]) -> str:
    """Convenience serializer used by the CLI with --json."""
    return json.dumps(as_jsonable(snapshots), indent=2)
