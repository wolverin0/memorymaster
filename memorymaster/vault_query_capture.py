"""Query-to-wiki capture — save high-quality query results as wiki pages.

When a query returns good results, the synthesized answer becomes a new
wiki page. This compounds knowledge: future queries can reference previous
answers without re-synthesizing.

Usage:
    memorymaster query "how does auth work" --save-to-vault
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SAFE_RE = re.compile(r"[^a-z0-9_-]+")


def _slugify(text: str) -> str:
    """Convert query text to a safe filename."""
    slug = _SAFE_RE.sub("-", text.lower()).strip("-")
    return slug[:60] or "query"


def capture_query_result(
    query_text: str,
    claims: list[dict],
    vault_dir: str | Path,
    scope: str = "global",
) -> dict[str, Any]:
    """Save query results as a wiki page.

    Only captures if there are >= 2 claims with avg confidence > 0.5.

    Returns: {captured: bool, file: str|None}
    """
    vault = Path(vault_dir)
    if not vault.exists():
        return {"captured": False, "file": None, "reason": "vault not found"}

    if len(claims) < 2:
        return {"captured": False, "file": None, "reason": "too few results"}

    confidences = [c.get("confidence", 0) for c in claims]
    avg_conf = sum(confidences) / len(confidences) if confidences else 0
    if avg_conf < 0.5:
        return {"captured": False, "file": None, "reason": "low confidence"}

    # Build the wiki page
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    slug = _slugify(query_text)
    scope_dir_name = _SAFE_RE.sub("-", scope.lower().replace(":", "-")).strip("-") or "global"

    lines = ["---"]
    lines.append(f'query: "{query_text[:100]}"')
    lines.append(f"captured_at: {now}")
    lines.append(f"claims_count: {len(claims)}")
    lines.append(f"avg_confidence: {avg_conf:.3f}")
    lines.append(f"scope: {scope}")
    lines.append("type: query-synthesis")
    lines.append("---")
    lines.append("")
    lines.append(f"# Q: {query_text[:100]}")
    lines.append("")
    lines.append(f"*Synthesized from {len(claims)} claims (avg conf={avg_conf:.2f})*")
    lines.append("")

    # Group claims by topic if they have subjects
    by_subject: dict[str, list[dict]] = {}
    for c in claims:
        subj = c.get("subject") or "general"
        by_subject.setdefault(subj, []).append(c)

    for subject, subject_claims in by_subject.items():
        if len(by_subject) > 1:
            lines.append(f"## {subject}")
            lines.append("")

        for c in sorted(subject_claims, key=lambda x: -x.get("confidence", 0)):
            hid = c.get("human_id") or f"claim-{c.get('id', '?')}"
            conf = c.get("confidence", 0)
            text = str(c.get("text", ""))[:300]
            pred = c.get("predicate") or ""
            obj_val = c.get("object_value") or ""

            lines.append(f"- **{hid}** (conf={conf:.2f})")
            if pred and obj_val:
                lines.append(f"  {pred} = {obj_val}")
            lines.append(f"  {text}")
            lines.append("")

    lines.append("---")
    lines.append(f"*Auto-captured by MemoryMaster on {now}*")

    # Write to queries/ subdirectory
    queries_dir = vault / "queries"
    queries_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{slug}.md"
    filepath = queries_dir / filename
    filepath.write_text("\n".join(lines), encoding="utf-8")

    logger.info("Captured query result: %s (%d claims)", filename, len(claims))
    return {"captured": True, "file": str(filepath)}
