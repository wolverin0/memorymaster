"""Vault linter — detect contradictions, orphans, gaps, and stale claims.

Implements Karpathy's "lint" operation: periodic health checks on the knowledge base
to ensure consistency, completeness, and freshness.

Usage:
    memorymaster lint-vault
    memorymaster lint-vault --scope project:pedrito --fix
"""
from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default vault root for STALE_ARTICLE detection. Callers can override via
# `lint_vault(wiki_root=...)` — the check is skipped silently if the path does
# not exist, so this stays inert on setups without a wiki.
_DEFAULT_WIKI_ROOT = Path("obsidian-vault/wiki")


def _load_claims(db_path: str, scope_filter: str | None = None) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    query = """SELECT id, text, claim_type, subject, predicate, object_value,
               scope, confidence, status, created_at, updated_at, human_id
               FROM claims WHERE status IN ('confirmed', 'candidate')"""
    params: list[Any] = []
    if scope_filter:
        query += " AND scope LIKE ?"
        params.append(f"{scope_filter}%")
    query += " ORDER BY updated_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _detect_contradictions(claims: list[dict]) -> list[dict]:
    """Find claims with same subject+predicate but different object_value."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in claims:
        if c["subject"] and c["predicate"]:
            key = f"{c['subject']}|{c['predicate']}"
            groups[key].append(c)

    contradictions = []
    for key, group in groups.items():
        if len(group) < 2:
            continue
        values = set()
        for c in group:
            val = (c.get("object_value") or c["text"][:80]).strip().lower()
            values.add(val)
        if len(values) > 1:
            contradictions.append({
                "type": "contradiction",
                "key": key,
                "claims": [
                    {"id": c["id"], "human_id": c.get("human_id"), "value": c.get("object_value") or c["text"][:80], "confidence": c["confidence"]}
                    for c in sorted(group, key=lambda x: -x["confidence"])
                ],
            })
    return contradictions


def _detect_orphans(claims: list[dict]) -> list[dict]:
    """Find claims with no subject, no predicate, and no links to anything."""
    orphans = []
    all_subjects = {c["subject"] for c in claims if c["subject"]}
    for c in claims:
        if not c["subject"] and not c["predicate"]:
            orphans.append({
                "type": "orphan",
                "id": c["id"],
                "human_id": c.get("human_id"),
                "text": c["text"][:100],
                "reason": "no subject or predicate",
            })
        elif c["subject"] and c["subject"] not in all_subjects:
            # Subject referenced only once — weak link
            count = sum(1 for other in claims if other["subject"] == c["subject"])
            if count == 1:
                orphans.append({
                    "type": "weak_link",
                    "id": c["id"],
                    "human_id": c.get("human_id"),
                    "subject": c["subject"],
                    "text": c["text"][:100],
                    "reason": "subject appears only once",
                })
    return orphans[:50]  # Cap at 50


def _detect_gaps(claims: list[dict]) -> list[dict]:
    """Find subjects mentioned in claim text but without their own claims."""
    subject_set = {c["subject"].lower() for c in claims if c["subject"]}

    # Extract potential entity references from claim texts
    mentioned: dict[str, int] = defaultdict(int)
    for c in claims:
        text = c["text"].lower()
        for word_group in [
            "mercadopago", "supabase", "whatsapp", "qdrant", "obsidian",
            "openclaw", "gitnexus", "memorymaster", "docker", "caddy",
            "playwright", "ollama", "gemini", "openai", "anthropic",
        ]:
            if word_group in text and word_group not in subject_set:
                mentioned[word_group] += 1

    gaps = []
    for entity, count in sorted(mentioned.items(), key=lambda x: -x[1]):
        if count >= 3:
            gaps.append({
                "type": "gap",
                "entity": entity,
                "mentions": count,
                "reason": f"mentioned in {count} claims but has no dedicated subject",
            })
    return gaps[:20]


def _detect_stale(claims: list[dict], max_age_days: int = 30) -> list[dict]:
    """Find confirmed claims that haven't been updated in a long time."""
    now = datetime.now(timezone.utc)
    stale = []
    for c in claims:
        if c["status"] != "confirmed":
            continue
        try:
            updated = datetime.fromisoformat(c["updated_at"])
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            age = (now - updated).days
            if age > max_age_days and c["confidence"] < 0.7:
                stale.append({
                    "type": "stale",
                    "id": c["id"],
                    "human_id": c.get("human_id"),
                    "age_days": age,
                    "confidence": c["confidence"],
                    "text": c["text"][:80],
                })
        except (ValueError, TypeError):
            continue

    return sorted(stale, key=lambda x: -x["age_days"])[:30]


def _detect_stale_articles(
    wiki_root: Path | str,
    *,
    threshold: float | None = None,
) -> list[dict]:
    """Flag wiki articles whose absorb-recency freshness is below ``threshold``.

    Implements roadmap item 11.8 (Option A) at the lint layer. Delegates the
    actual scoring to :mod:`memorymaster.wiki_freshness` and wraps each stale
    article in a structured warning row. Warning only — never blocks lint.
    """
    try:
        from memorymaster.wiki_freshness import (
            STALE_ARTICLE_THRESHOLD,
            scan_vault,
        )
    except ImportError:  # pragma: no cover — defensive, same package
        return []

    cutoff = STALE_ARTICLE_THRESHOLD if threshold is None else float(threshold)
    root = Path(wiki_root)
    if not root.exists():
        return []

    stale_articles: list[dict] = []
    for snap in scan_vault(root):
        if snap.freshness_score >= cutoff:
            continue
        stale_articles.append(
            {
                "type": "stale_article",
                "path": str(snap.path),
                "title": snap.title,
                "scope": snap.scope,
                "days_since_absorb": round(snap.days_since_absorb, 2),
                "freshness_score": round(snap.freshness_score, 4),
                "reason": (
                    f"not absorbed in {snap.days_since_absorb:.0f}d "
                    f"(freshness={snap.freshness_score:.2f} < {cutoff:.2f})"
                ),
            }
        )
    return stale_articles


def _llm_verify_contradictions(contradictions: list[dict]) -> list[dict]:
    """Use LLM to verify if detected contradictions are real or false positives."""
    if not contradictions:
        return contradictions

    try:
        from memorymaster.llm_provider import call_llm, parse_json_response
    except ImportError:
        return contradictions

    prompt = """You are a knowledge base auditor. For each potential contradiction below,
determine if it's a REAL contradiction (the claims actually disagree) or a FALSE POSITIVE
(they describe different aspects/times/contexts of the same thing).

Return a JSON array: [{"key": "<key>", "real": true/false, "explanation": "brief reason"}]
Return ONLY valid JSON."""

    batch_text = "\n".join(
        "KEY={}: {} vs {}".format(
            c["key"],
            c["claims"][0]["value"][:80],
            c["claims"][1]["value"][:80] if len(c["claims"]) > 1 else "N/A",
        )
        for c in contradictions[:15]
    )

    response = call_llm(prompt, batch_text)
    verdicts = parse_json_response(response)

    verdict_map = {v["key"]: v for v in verdicts if isinstance(v, dict) and "key" in v}
    for c in contradictions:
        v = verdict_map.get(c["key"])
        if v:
            c["verified"] = v.get("real", True)
            c["explanation"] = v.get("explanation", "")

    return contradictions


def lint_vault(
    db_path: str,
    *,
    scope_filter: str | None = None,
    verify_with_llm: bool = True,
    max_stale_days: int = 30,
    wiki_root: Path | str | None = None,
) -> dict[str, Any]:
    """Run lint checks on the knowledge base.

    Returns a report with contradictions, orphans, gaps, stale claims, and
    stale wiki articles (Option A absorb-recency).
    """
    claims = _load_claims(db_path, scope_filter)

    resolved_wiki_root = Path(wiki_root) if wiki_root else _DEFAULT_WIKI_ROOT
    stale_articles = _detect_stale_articles(resolved_wiki_root)

    if not claims:
        return {
            "claims": 0,
            "issues": len(stale_articles),
            "contradictions": [],
            "orphans": [],
            "gaps": [],
            "stale": [],
            "stale_articles": stale_articles,
        }

    contradictions = _detect_contradictions(claims)
    if verify_with_llm and contradictions:
        contradictions = _llm_verify_contradictions(contradictions)
        # Keep only verified real contradictions
        contradictions = [c for c in contradictions if c.get("verified", True)]

    orphans = _detect_orphans(claims)
    gaps = _detect_gaps(claims)
    stale = _detect_stale(claims, max_stale_days)

    total_issues = (
        len(contradictions)
        + len(orphans)
        + len(gaps)
        + len(stale)
        + len(stale_articles)
    )

    report = {
        "claims": len(claims),
        "issues": total_issues,
        "contradictions": contradictions,
        "orphans": orphans,
        "gaps": gaps,
        "stale": stale,
        "stale_articles": stale_articles,
    }

    logger.info(
        "Lint: %d claims, %d issues found (%d stale articles)",
        len(claims),
        total_issues,
        len(stale_articles),
    )
    return report
