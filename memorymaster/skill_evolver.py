"""Skill evolution — extract procedural knowledge from session patterns.

Inspired by MetaClaw's SkillEvolver. Analyzes conversation patterns
(via feedback data) and generates procedure-type claims that capture
"how to do things" rather than just "what we know."

When tasks fail or get corrected, this module extracts the lesson
as a procedure claim: "When X happens, do Y because Z."

Usage:
    memorymaster evolve-skills
    memorymaster evolve-skills --min-feedback 50
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "deepseek-coder-v2:16b"

EVOLUTION_PROMPT = """Analyze these conversation patterns from a memory system and extract PROCEDURAL SKILLS — reusable lessons about how to do things better.

Each skill should be a specific, actionable instruction that an AI agent can follow in future sessions.

Feedback data (which claims were useful vs ignored):
{feedback_summary}

High-quality claims (frequently accessed):
{top_claims}

Low-quality claims (never accessed):
{bottom_claims}

Generate 1-3 new procedural skills as JSON:
{{"skills": [{{"text": "When [situation], do [action] because [reason]", "category": "coding|debugging|architecture|deployment|communication"}}]}}

Rules:
- Only extract skills that are clearly supported by the data
- Each skill must be actionable (not just a fact)
- Focus on patterns that repeat across multiple claims
- Return empty array if no clear skills can be extracted"""


def _llm_generate(prompt: str) -> list[dict]:
    """Call LLM to generate skills."""
    url = (os.environ.get("OLLAMA_URL") or DEFAULT_OLLAMA_URL).rstrip("/")
    model = os.environ.get("EVOLVER_LLM_MODEL") or DEFAULT_MODEL

    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 500},
    }).encode()

    req = urllib.request.Request(
        f"{url}/api/chat", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            raw = result.get("message", {}).get("content", "")
            text = raw.strip()
            if text.startswith("```"):
                lines = [line for line in text.split("\n") if not line.strip().startswith("```")]
                text = "\n".join(lines)
            data = json.loads(text)
            return data.get("skills", [])
    except Exception as exc:
        logger.warning("Skill evolution LLM call failed: %s", exc)
        return []


def evolve_skills(db_path: str, *, min_feedback: int = 20) -> dict:
    """Analyze feedback patterns and generate procedural skill claims.

    Returns: {"generated": int, "ingested": int, "skipped_reason": str | None}
    """
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        # Check if enough feedback exists
        try:
            feedback_count = conn.execute("SELECT COUNT(*) FROM usage_feedback").fetchone()[0]
        except sqlite3.OperationalError:
            return {"generated": 0, "ingested": 0, "skipped_reason": "no feedback table"}

        if feedback_count < min_feedback:
            return {"generated": 0, "ingested": 0, "skipped_reason": f"insufficient feedback ({feedback_count}/{min_feedback})"}

        # Get top claims (frequently returned)
        top = conn.execute("""
            SELECT uf.claim_id, COUNT(*) as cnt, c.text
            FROM usage_feedback uf JOIN claims c ON c.id = uf.claim_id
            GROUP BY uf.claim_id ORDER BY cnt DESC LIMIT 10
        """).fetchall()

        # Get bottom claims (never returned despite existing)
        bottom = conn.execute("""
            SELECT c.id, c.text FROM claims c
            WHERE c.status = 'confirmed' AND c.access_count = 0
            ORDER BY c.created_at DESC LIMIT 10
        """).fetchall()

        # Build prompt
        top_summary = "\n".join(f"- [{r['cnt']}x returned] {r['text'][:100]}" for r in top)
        bottom_summary = "\n".join(f"- [never accessed] {r['text'][:100]}" for r in bottom)
        feedback_summary = f"{feedback_count} total feedback rows, {len(top)} frequently used claims"

        prompt = EVOLUTION_PROMPT.format(
            feedback_summary=feedback_summary,
            top_claims=top_summary or "(none)",
            bottom_claims=bottom_summary or "(none)",
        )

    finally:
        conn.close()

    # Generate skills via LLM
    skills = _llm_generate(prompt)
    if not skills:
        return {"generated": 0, "ingested": 0, "skipped_reason": "llm returned no skills"}

    # Ingest as procedure claims
    from memorymaster.models import CitationInput
    from memorymaster.service import MemoryService
    from pathlib import Path

    svc = MemoryService(db_target=db_path, workspace_root=Path.cwd())
    ingested = 0

    for skill in skills:
        text = skill.get("text", "").strip()
        if not text or len(text) < 10:
            continue
        category = skill.get("category", "general")
        try:
            svc.ingest(
                text=text,
                citations=[CitationInput(source="skill-evolver", locator=category)],
                claim_type="procedure",
                scope="global",
                confidence=0.7,
                source_agent="skill-evolver",
                idempotency_key=f"evolved-{hash(text)}",
            )
            ingested += 1
        except Exception as exc:
            logger.warning("Failed to ingest evolved skill: %s", exc)

    return {"generated": len(skills), "ingested": ingested, "skipped_reason": None}
