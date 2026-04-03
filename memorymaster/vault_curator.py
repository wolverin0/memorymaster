"""Vault curator — LLM-powered organization of claims into a hierarchical Obsidian vault.

Instead of flat files per claim, groups claims by topic into organized markdown files
with wikilinks, creating a navigable knowledge graph.

Usage:
    memorymaster curate-vault --output ./obsidian-vault/
    memorymaster curate-vault --output ./vault/ --scope project:pedrito --dry-run
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SAFE_RE = re.compile(r"[^a-z0-9_-]+")

CATEGORIZE_PROMPT = """You are a knowledge curator. Given a batch of claims from a coding project, categorize each into a topic.

Available topics: architecture, bugs, decisions, constraints, integrations, deployment, auth, api, database, frontend, testing, performance, security, config, workflow, other

For each claim, return its ID and topic. Return a JSON array:
[{"id": <claim_id>, "topic": "<topic>"}]

Rules:
- Pick the SINGLE most relevant topic
- "decisions" = choices made ("we chose X over Y")
- "constraints" = rules/limits ("never do X", "must always Y")
- "bugs" = root causes, fixes, errors
- "integrations" = third-party services (MercadoPago, WhatsApp, Supabase, etc.)
- "config" = env vars, setup, installation
- "workflow" = processes, pipelines, CI/CD
- Return ONLY valid JSON array, no explanation"""


def _safe_name(text: str) -> str:
    return _SAFE_RE.sub("-", text.lower()).strip("-")[:60] or "misc"


def _scope_dirname(scope: str) -> str:
    parts = scope.split(":")
    name = "-".join(parts[:2]) if len(parts) >= 2 else parts[0]
    return _SAFE_RE.sub("-", name.lower()).strip("-") or "default"


def _load_claims(db_path: str, scope_filter: str | None = None) -> list[dict]:
    """Load confirmed + candidate claims from DB."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    query = """SELECT id, text, claim_type, subject, predicate, object_value,
               scope, confidence, status, created_at, updated_at, human_id
               FROM claims WHERE status IN ('confirmed', 'candidate')"""
    params: list[Any] = []

    if scope_filter:
        query += " AND scope LIKE ?"
        params.append(f"{scope_filter}%")

    query += " ORDER BY confidence DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _batch_categorize(claims: list[dict], batch_size: int = 20) -> dict[int, str]:
    """Call LLM to categorize claims into topics. Returns {claim_id: topic}."""
    from memorymaster.llm_provider import call_llm, parse_json_response

    result: dict[int, str] = {}

    for i in range(0, len(claims), batch_size):
        batch = claims[i:i + batch_size]
        claims_text = "\n".join(
            "ID={}: {}".format(c["id"], str(c["text"])[:150].encode("ascii", errors="replace").decode("ascii"))
            for c in batch
        )

        response = call_llm(CATEGORIZE_PROMPT, claims_text)
        parsed = parse_json_response(response)

        for item in parsed:
            if isinstance(item, dict) and "id" in item and "topic" in item:
                result[item["id"]] = item["topic"]

    return result


def _group_by_scope_and_topic(
    claims: list[dict], categories: dict[int, str]
) -> dict[str, dict[str, list[dict]]]:
    """Group claims into {scope: {topic: [claims]}}."""
    tree: dict[str, dict[str, list[dict]]] = {}
    for c in claims:
        scope_dir = _scope_dirname(c["scope"])
        topic = categories.get(c["id"], "other")
        tree.setdefault(scope_dir, {}).setdefault(topic, []).append(c)
    return tree


def _render_topic_file(topic: str, claims: list[dict], all_claims_by_id: dict[int, dict]) -> str:
    """Render a topic file with all claims grouped, with wikilinks."""
    lines = ["---"]
    lines.append(f"topic: {topic}")
    lines.append(f"claims: {len(claims)}")
    lines.append(f"curated_at: {datetime.now(timezone.utc).isoformat()}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {topic.title()}")
    lines.append("")
    lines.append(f"{len(claims)} claims in this topic.")
    lines.append("")

    # Sort by confidence descending
    sorted_claims = sorted(claims, key=lambda c: -c["confidence"])

    for c in sorted_claims:
        hid = c.get("human_id") or f"claim-{c['id']}"
        conf = c["confidence"]
        status = c["status"]
        claim_type = c.get("claim_type") or "fact"

        # Status icon
        icon = {"confirmed": "✓", "candidate": "?", "stale": "~"}.get(status, "·")

        lines.append(f"## {icon} {hid} ({claim_type}, conf={conf:.2f})")
        lines.append("")

        # Subject/predicate/value tuple
        subj = c.get("subject") or ""
        pred = c.get("predicate") or ""
        obj_val = c.get("object_value") or ""
        if subj or pred:
            lines.append(f"**{subj}** / {pred}" + (f" = {obj_val}" if obj_val else ""))
            lines.append("")

        # Claim text (truncated to keep vault readable)
        text = str(c["text"])[:500]
        lines.append(text)
        lines.append("")

        # Wikilinks to same-subject claims in other topics
        if subj:
            related = [
                other for other in all_claims_by_id.values()
                if other.get("subject") == subj and other["id"] != c["id"]
            ][:3]
            if related:
                links = ", ".join(
                    "[[{}/{}|{}]]".format(
                        _scope_dirname(r["scope"]),
                        _get_topic(r["id"]),
                        r.get("human_id") or "claim-{}".format(r["id"]),
                    )
                    for r in related
                )
                lines.append(f"Related: {links}")
                lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# Module-level cache for topic lookup in wikilinks
_topic_cache: dict[int, str] = {}


def _get_topic(claim_id: int) -> str:
    return _topic_cache.get(claim_id, "other")


def _render_scope_index(scope_dir: str, topics: dict[str, list[dict]]) -> str:
    """Render index file for a project scope."""
    total = sum(len(cs) for cs in topics.values())
    lines = [f"# {scope_dir}", ""]
    lines.append(f"Total: {total} claims across {len(topics)} topics.")
    lines.append("")
    lines.append("## Topics")
    lines.append("")
    for topic in sorted(topics.keys()):
        count = len(topics[topic])
        lines.append(f"- [[{scope_dir}/{topic}|{topic.title()}]] ({count} claims)")
    lines.append("")
    return "\n".join(lines)


def _render_root_index(tree: dict[str, dict[str, list[dict]]]) -> str:
    """Render root vault index."""
    total = sum(
        sum(len(cs) for cs in topics.values())
        for topics in tree.values()
    )
    lines = ["# MemoryMaster Curated Vault", ""]
    lines.append(f"Curated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"Total: {total} claims across {len(tree)} projects.")
    lines.append("")
    lines.append("## Projects")
    lines.append("")
    for scope_dir in sorted(tree.keys()):
        topics = tree[scope_dir]
        count = sum(len(cs) for cs in topics.values())
        topic_list = ", ".join(sorted(topics.keys()))
        lines.append(f"- [[{scope_dir}/_index|{scope_dir}]] — {count} claims ({topic_list})")
    lines.append("")
    return "\n".join(lines)


def curate_vault(
    db_path: str,
    output_dir: str | Path,
    *,
    scope_filter: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Curate claims into an organized Obsidian vault using LLM categorization.

    Returns stats: {claims, topics, files_written, scopes}
    """
    global _topic_cache

    output = Path(output_dir)
    claims = _load_claims(db_path, scope_filter)
    if not claims:
        return {"claims": 0, "topics": 0, "files_written": 0, "scopes": 0}

    logger.info("Curating %d claims with LLM...", len(claims))

    # Categorize with LLM
    categories = _batch_categorize(claims)
    _topic_cache = categories

    # Build lookup
    all_claims_by_id = {c["id"]: c for c in claims}

    # Group into tree
    tree = _group_by_scope_and_topic(claims, categories)

    if dry_run:
        topic_counts: dict[str, int] = {}
        for topics in tree.values():
            for topic, cs in topics.items():
                topic_counts[topic] = topic_counts.get(topic, 0) + len(cs)
        return {
            "claims": len(claims),
            "scopes": len(tree),
            "topics": len(topic_counts),
            "topic_breakdown": topic_counts,
            "files_written": 0,
            "dry_run": True,
        }

    # Write vault
    files_written = 0
    all_topics: set[str] = set()

    for scope_dir, topics in tree.items():
        scope_path = output / scope_dir
        scope_path.mkdir(parents=True, exist_ok=True)

        for topic, topic_claims in topics.items():
            all_topics.add(topic)
            content = _render_topic_file(topic, topic_claims, all_claims_by_id)
            (scope_path / f"{topic}.md").write_text(content, encoding="utf-8")
            files_written += 1

        # Scope index
        idx_content = _render_scope_index(scope_dir, topics)
        (scope_path / "_index.md").write_text(idx_content, encoding="utf-8")
        files_written += 1

    # Root index
    root_idx = _render_root_index(tree)
    (output / "index.md").write_text(root_idx, encoding="utf-8")
    files_written += 1

    # Timestamp
    (output / ".last_curate").write_text(
        datetime.now(timezone.utc).isoformat(), encoding="utf-8"
    )

    stats = {
        "claims": len(claims),
        "scopes": len(tree),
        "topics": len(all_topics),
        "files_written": files_written,
    }
    logger.info("Vault curated: %s", stats)
    return stats
