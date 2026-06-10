"""Cross-source synthesis — update vault wiki pages when new claims arrive.

When a new claim is ingested, find related topic files in the vault and
update them with the new information. Implements Karpathy's insight that
each ingest should touch ~3-5 existing pages.

Usage:
    Called automatically after ingest_claim when vault exists.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SAFE_RE = re.compile(r"[^a-z0-9_-]+")


def _scope_dirname(scope: str) -> str:
    parts = scope.split(":")
    name = "-".join(parts[:2]) if len(parts) >= 2 else parts[0]
    return _SAFE_RE.sub("-", name.lower()).strip("-") or "default"


def _find_related_topic_files(vault_dir: Path, scope: str, subject: str | None) -> list[Path]:
    """Find topic files in the vault that might be related to this claim."""
    scope_dir = vault_dir / _scope_dirname(scope)
    if not scope_dir.exists():
        return []

    related = []
    for md_file in scope_dir.glob("*.md"):
        if md_file.name.startswith("_"):
            continue
        # Check if the file mentions the subject
        if subject:
            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
                if subject.lower() in content.lower():
                    related.append(md_file)
            except Exception:
                continue
    return related[:5]  # Cap at 5 files


def _append_to_topic_file(topic_file: Path, claim: dict) -> bool:
    """Append a new claim entry to an existing topic file."""
    try:
        content = topic_file.read_text(encoding="utf-8", errors="replace")

        hid = claim.get("human_id") or f"claim-{claim['id']}"
        conf = claim.get("confidence", 0.5)
        claim_type = claim.get("claim_type") or "fact"
        subj = claim.get("subject") or ""
        pred = claim.get("predicate") or ""
        obj_val = claim.get("object_value") or ""
        text = str(claim.get("text", ""))[:500]

        # Check if already present
        if hid in content:
            return False

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_entry = f"\n## + {hid} ({claim_type}, conf={conf:.2f}) — added {now}\n\n"
        if subj or pred:
            new_entry += f"**{subj}** / {pred}"
            if obj_val:
                new_entry += f" = {obj_val}"
            new_entry += "\n\n"
        new_entry += f"{text}\n\n---\n"

        # Append before the last ---
        content = content.rstrip()
        content += "\n" + new_entry

        topic_file.write_text(content, encoding="utf-8")
        return True
    except Exception as e:
        logger.debug("Failed to update %s: %s", topic_file, e)
        return False


def _update_scope_index(vault_dir: Path, scope: str) -> None:
    """Update the _index.md for a scope to reflect new content."""
    scope_dir = vault_dir / _scope_dirname(scope)
    index_file = scope_dir / "_index.md"
    if not scope_dir.exists():
        return

    topics = {}
    for md_file in scope_dir.glob("*.md"):
        if md_file.name.startswith("_"):
            continue
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
            # Count ## entries (claims)
            claim_count = content.count("\n## ")
            topics[md_file.stem] = claim_count
        except Exception:
            continue

    if not topics:
        return

    total = sum(topics.values())
    lines = [f"# {_scope_dirname(scope)}", ""]
    lines.append(f"Total: {total} claims across {len(topics)} topics.")
    lines.append(f"Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append("")
    lines.append("## Topics")
    lines.append("")
    for topic in sorted(topics.keys()):
        count = topics[topic]
        scope_name = _scope_dirname(scope)
        lines.append(f"- [[{scope_name}/{topic}|{topic.title()}]] ({count} claims)")
    lines.append("")

    index_file.write_text("\n".join(lines), encoding="utf-8")


def synthesize_on_ingest(
    claim: dict,
    vault_dir: str | Path,
) -> dict[str, Any]:
    """After a claim is ingested, update related vault pages.

    Returns stats: {related_found, pages_updated}
    """
    vault = Path(vault_dir)
    if not vault.exists():
        return {"related_found": 0, "pages_updated": 0}

    scope = claim.get("scope", "project")
    subject = claim.get("subject")

    # Find related topic files
    related = _find_related_topic_files(vault, scope, subject)

    pages_updated = 0
    for topic_file in related:
        if _append_to_topic_file(topic_file, claim):
            pages_updated += 1

    # Update scope index if we changed anything
    if pages_updated:
        _update_scope_index(vault, scope)

    return {"related_found": len(related), "pages_updated": pages_updated}
