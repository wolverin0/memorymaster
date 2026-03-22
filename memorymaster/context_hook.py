"""Context hook — automatic memory extraction and injection for Claude Code.

Two functions:
  1. recall(query) — query memorymaster for relevant context before responding
  2. observe(text, source) — extract and ingest claims after a conversation turn

Designed to be called from Claude Code hooks or CLAUDE.md instructions.

Usage (CLI):
    memorymaster recall "what is the user working on?"
    memorymaster observe --text "User decided to use PostgreSQL" --source "session"
    memorymaster observe --stdin < conversation_turn.txt --source "session"

Usage (from CLAUDE.md):
    Before responding, run: memorymaster recall "<user message summary>"
    After important decisions: memorymaster observe --text "<decision>" --source "session"
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Patterns that indicate something worth remembering
OBSERVATION_PATTERNS = [
    # User corrections/preferences
    (r"\b(don'?t|never|always|stop|instead|prefer|please)\b.*", "preference"),
    # Decisions
    (r"\b(decided|decision|we('ll| will)|let'?s|going to|plan is)\b.*", "decision"),
    # Constraints
    (r"\b(must|require|rule|constraint|forbidden|mandatory|critical)\b.*", "constraint"),
    # Architecture/tech choices
    (r"\b(using|switched to|migrated|deployed|installed|configured)\b.*", "fact"),
    # Bug/issue patterns
    (r"\b(bug|fix|broke|crash|error|issue|problem|wrong)\b.*", "event"),
    # Commitments
    (r"\b(todo|will do|next step|action item|need to|should)\b.*", "commitment"),
]

_COMPILED_PATTERNS = [(re.compile(p, re.IGNORECASE), t) for p, t in OBSERVATION_PATTERNS]


def classify_observation(text: str) -> str | None:
    """Check if text contains something worth remembering. Returns claim_type or None."""
    for pattern, claim_type in _COMPILED_PATTERNS:
        if pattern.search(text):
            return claim_type
    return None


def recall(
    query: str,
    *,
    db_path: str = "",
    budget: int = 2000,
    format: str = "text",
) -> str:
    """Query memorymaster for relevant context. Returns formatted text."""
    from memorymaster.service import MemoryService

    db = db_path or os.environ.get("MEMORYMASTER_DEFAULT_DB") or "memorymaster.db"
    svc = MemoryService(db_target=db, workspace_root=Path.cwd())

    result = svc.query_for_context(
        query=query,
        token_budget=budget,
        output_format=format,
    )

    if result.claims_included == 0:
        return ""

    # Sanitize for Windows console encoding
    return result.output.encode("ascii", errors="replace").decode("ascii")


def observe(
    text: str,
    *,
    source: str = "session",
    db_path: str = "",
    scope: str = "project",
    auto_classify: bool = True,
    force: bool = False,
) -> dict:
    """Extract and ingest observations from conversation text.

    If auto_classify=True, only ingests text that matches observation patterns.
    If force=True, ingests regardless of pattern matching.

    Returns: {"ingested": bool, "claim_type": str, "claim_id": int | None}
    """
    # Check if worth remembering
    claim_type = None
    if auto_classify:
        claim_type = classify_observation(text)
        if claim_type is None and not force:
            return {"ingested": False, "claim_type": None, "claim_id": None, "reason": "no_pattern_match"}

    if not claim_type:
        claim_type = "fact"

    from memorymaster.models import CitationInput
    from memorymaster.service import MemoryService

    db = db_path or os.environ.get("MEMORYMASTER_DEFAULT_DB") or "memorymaster.db"
    svc = MemoryService(db_target=db, workspace_root=Path.cwd())

    try:
        claim = svc.ingest(
            text=text.strip()[:2000],
            citations=[CitationInput(source=source)],
            claim_type=claim_type,
            scope=scope,
            confidence=0.6,
            source_agent="context-hook",
        )
        return {"ingested": True, "claim_type": claim_type, "claim_id": claim.id}
    except Exception as exc:
        logger.warning("Observe failed: %s", exc)
        return {"ingested": False, "claim_type": claim_type, "claim_id": None, "reason": str(exc)}


def observe_llm(
    text: str,
    *,
    source: str = "session",
    db_path: str = "",
    scope: str = "project",
) -> dict:
    """Use LLM to extract structured claims from conversation text.

    More thorough than rule-based observe() but slower (~5s per call).
    """
    from memorymaster.auto_extractor import extract_claims_from_text
    from memorymaster.models import CitationInput
    from memorymaster.service import MemoryService

    db = db_path or os.environ.get("MEMORYMASTER_DEFAULT_DB") or "memorymaster.db"

    extracted = extract_claims_from_text(text, source=source, scope=scope)
    if not extracted:
        return {"ingested": 0, "extracted": 0}

    svc = MemoryService(db_target=db, workspace_root=Path.cwd())
    ingested = 0
    for claim_data in extracted:
        try:
            svc.ingest(
                text=claim_data.get("text", ""),
                citations=[CitationInput(source=source)],
                claim_type=claim_data.get("claim_type", "fact"),
                subject=claim_data.get("subject"),
                predicate=claim_data.get("predicate"),
                object_value=claim_data.get("object_value"),
                scope=scope,
                confidence=0.6,
                source_agent="context-hook-llm",
            )
            ingested += 1
        except Exception:
            pass

    return {"ingested": ingested, "extracted": len(extracted)}
