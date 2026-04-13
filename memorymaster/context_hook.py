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
    skip_qdrant: bool = False,
) -> str:
    """Query memorymaster for relevant context with quality ranking."""
    from memorymaster.service import MemoryService

    db = db_path or os.environ.get("MEMORYMASTER_DEFAULT_DB") or "memorymaster.db"
    svc = MemoryService(db_target=db, workspace_root=Path.cwd())

    # Get ranked results with lexical scoring
    rows = svc.query_rows(
        query_text=query,
        limit=8,
        retrieval_mode="legacy",
        include_candidates=True,
        scope_allowlist=None,
    )

    if not rows and not skip_qdrant:
        # Fallback to Qdrant semantic search
        try:
            from memorymaster.qdrant_backend import QdrantBackend
            backend = QdrantBackend()
            hits = backend.search(query, limit=5)
            backend.close()
            if hits:
                lines = ["# Memory Context (semantic)", ""]
                for hit in hits:
                    p = hit.get("payload", {})
                    text = p.get("claim_text", "")[:200]
                    lines.append(f"- {text}")
                return "\n".join(lines).encode("ascii", errors="replace").decode("ascii")
        except Exception:
            pass
        return ""

    if not rows:
        return ""

    # Re-rank by lexical relevance — claims with more query words score higher
    query_words = set(query.lower().split())

    def _relevance(row):
        claim = row.get("claim")
        text = (claim.text if hasattr(claim, "text") else "").lower()
        # Count how many query words appear in the claim
        matches = sum(1 for w in query_words if w in text and len(w) > 2)
        # Bonus: full query phrase appears in text
        phrase_bonus = 1.0 if query.lower() in text else 0.0
        # Bonus: ALL query words present (not just some)
        all_present = 1.0 if matches == len([w for w in query_words if len(w) > 2]) else 0.0
        lexical = row.get("lexical_score", 0)
        conf = row.get("confidence_score", 0)
        return matches * 0.3 + phrase_bonus * 0.3 + all_present * 0.2 + lexical * 0.1 + conf * 0.1

    ranked = sorted(rows, key=_relevance, reverse=True)

    # Build output — top claims within budget
    lines = ["# Memory Context", ""]
    tokens_used = 0
    chars_per_token = 4
    for row in ranked:
        claim = row.get("claim")
        if not hasattr(claim, "text"):
            continue
        text = claim.text[:300]
        wiki_slug = getattr(claim, "wiki_article", None)
        if wiki_slug:
            chunk = f"- {text}  (compiled in [[{wiki_slug}]])"
        else:
            chunk = f"- {text}"
        chunk_tokens = len(chunk) // chars_per_token
        if tokens_used + chunk_tokens > budget:
            break
        lines.append(chunk)
        tokens_used += chunk_tokens

    if len(lines) <= 2:
        return ""

    return "\n".join(lines).encode("ascii", errors="replace").decode("ascii")


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
