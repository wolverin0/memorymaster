"""Obsidian vault exporter — writes claims as linked .md files.

Usage:
    memorymaster export-vault --output ./obsidian-vault/
    memorymaster export-vault --output ./vault/ --scope project:pedrito
    memorymaster export-vault --output ./vault/ --confirmed-only
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memorymaster.models import Claim

logger = logging.getLogger(__name__)

_SAFE_FILENAME_RE = re.compile(r"[^a-z0-9_-]+")


def _safe_dirname(scope: str) -> str:
    """Convert scope like 'project:pedrito:abc123' to 'project-pedrito'."""
    parts = scope.split(":")
    name = "-".join(parts[:2]) if len(parts) >= 2 else parts[0]
    return _SAFE_FILENAME_RE.sub("-", name.lower()).strip("-") or "default"


def _claim_to_markdown(claim: Claim, links: list[dict[str, Any]] | None = None) -> str:
    """Render a claim as Obsidian-flavored Markdown with YAML frontmatter."""
    lines = ["---"]
    lines.append(f"claim_id: {claim.id}")
    if claim.human_id:
        lines.append(f"human_id: {claim.human_id}")
    lines.append(f"status: {claim.status}")
    lines.append(f"confidence: {claim.confidence:.3f}")
    if claim.claim_type:
        lines.append(f"type: {claim.claim_type}")
    lines.append(f"scope: {claim.scope}")
    lines.append(f"volatility: {claim.volatility}")
    if claim.subject:
        lines.append(f"subject: \"{claim.subject}\"")
    if claim.predicate:
        lines.append(f"predicate: \"{claim.predicate}\"")
    if claim.object_value:
        lines.append(f"object: \"{claim.object_value}\"")
    lines.append(f"pinned: {str(claim.pinned).lower()}")
    lines.append(f"created_at: {claim.created_at}")
    lines.append(f"updated_at: {claim.updated_at}")
    lines.append("---")
    lines.append("")

    # Title
    title = claim.text[:80].replace("\n", " ").strip()
    lines.append(f"# {title}")
    lines.append("")

    # Body
    lines.append(claim.text)
    lines.append("")

    # Links as wikilinks
    if links:
        lines.append("## Links")
        for link in links:
            link_type = link.get("link_type", "relates_to")
            target_human_id = link.get("target_human_id", f"claim-{link.get('target_id', '?')}")
            lines.append(f"- {link_type} [[{target_human_id}]]")
        lines.append("")

    # Citations
    if claim.citations:
        lines.append("## Citations")
        for cite in claim.citations:
            locator = f" | {cite.locator}" if cite.locator else ""
            excerpt = f" | {cite.excerpt}" if cite.excerpt else ""
            lines.append(f"- `{cite.source}{locator}{excerpt}`")
        lines.append("")

    return "\n".join(lines)


def export_vault(
    store,
    output_dir: str | Path,
    *,
    scope_filter: str | None = None,
    confirmed_only: bool = False,
    include_archived: bool = False,
) -> dict[str, int]:
    """Export claims as Obsidian-compatible .md files.

    Parameters
    ----------
    store : SQLiteStore | PostgresStore
    output_dir : path to write .md files
    scope_filter : only export claims matching this scope prefix
    confirmed_only : only export confirmed claims
    include_archived : include archived claims

    Returns
    -------
    dict with keys: exported, skipped, directories_created
    """
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    stats = {"exported": 0, "skipped": 0, "directories_created": 0}
    seen_dirs: set[str] = set()

    # Fetch all claims
    status_filter = "confirmed" if confirmed_only else None
    claims = store.list_claims(
        status=status_filter,
        limit=50_000,
        include_archived=include_archived,
        include_citations=True,
    )

    # Build human_id lookup for link resolution
    human_id_map: dict[int, str] = {}
    for c in claims:
        hid = getattr(c, "human_id", None) or f"claim-{c.id}"
        human_id_map[c.id] = hid

    for claim in claims:
        # Scope filter
        if scope_filter and not claim.scope.startswith(scope_filter):
            stats["skipped"] += 1
            continue

        # Determine output subdirectory
        scope_dir = _safe_dirname(claim.scope)
        claim_dir = output / scope_dir
        if scope_dir not in seen_dirs:
            claim_dir.mkdir(parents=True, exist_ok=True)
            seen_dirs.add(scope_dir)
            stats["directories_created"] += 1

        # Get links
        links_raw = []
        try:
            raw_links = store.get_claim_links(claim.id)
            for link in raw_links:
                target_id = link.target_id if link.source_id == claim.id else link.source_id
                links_raw.append({
                    "link_type": link.link_type,
                    "target_id": target_id,
                    "target_human_id": human_id_map.get(target_id, f"claim-{target_id}"),
                })
        except Exception:
            pass  # links table might not exist on old DBs

        # Render and write
        human_id = getattr(claim, "human_id", None) or f"claim-{claim.id}"
        filename = f"{human_id}.md"
        md_content = _claim_to_markdown(claim, links_raw)
        (claim_dir / filename).write_text(md_content, encoding="utf-8")
        stats["exported"] += 1

    # Write index
    _write_index(output, claims, human_id_map, scope_filter)

    logger.info(
        "Vault export: %d exported, %d skipped, %d dirs",
        stats["exported"], stats["skipped"], stats["directories_created"],
    )
    return stats


def _write_index(
    output: Path,
    claims: list[Claim],
    human_id_map: dict[int, str],
    scope_filter: str | None,
) -> None:
    """Write an index.md with links to all exported claims."""
    lines = ["# MemoryMaster Vault Index", ""]
    lines.append(f"Exported: {datetime.now(timezone.utc).isoformat()}")
    if scope_filter:
        lines.append(f"Scope filter: `{scope_filter}`")
    lines.append(f"Total claims: {len(claims)}")
    lines.append("")

    # Group by scope
    by_scope: dict[str, list[Claim]] = {}
    for c in claims:
        if scope_filter and not c.scope.startswith(scope_filter):
            continue
        by_scope.setdefault(c.scope, []).append(c)

    for scope in sorted(by_scope.keys()):
        scope_claims = by_scope[scope]
        lines.append(f"## {scope} ({len(scope_claims)} claims)")
        lines.append("")
        for c in sorted(scope_claims, key=lambda x: -x.confidence)[:20]:
            hid = human_id_map.get(c.id, f"claim-{c.id}")
            scope_dir = _safe_dirname(c.scope)
            status_icon = {"confirmed": "v", "candidate": "?", "stale": "~", "conflicted": "!"}.get(c.status, "-")
            lines.append(f"- [{status_icon}] [[{scope_dir}/{hid}|{c.text[:60]}]] (conf={c.confidence:.2f})")
        if len(scope_claims) > 20:
            lines.append(f"- ... and {len(scope_claims) - 20} more")
        lines.append("")

    (output / "index.md").write_text("\n".join(lines), encoding="utf-8")
