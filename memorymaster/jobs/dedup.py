"""Deduplication engine: detect and merge duplicate claims using embedding similarity."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from memorymaster.embeddings import EmbeddingProvider, cosine_similarity, create_best_provider
from memorymaster.lifecycle import transition_claim
from memorymaster.models import Claim

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DuplicatePair:
    """A pair of claims identified as duplicates."""

    keep_id: int
    archive_id: int
    similarity: float
    text_overlap: float
    keep_confidence: float
    archive_confidence: float
    keep_text: str
    archive_text: str


@dataclass(slots=True)
class DedupResult:
    """Summary of a dedup run."""

    scanned: int = 0
    duplicates_found: int = 0
    claims_archived: int = 0
    pairs: list[DuplicatePair] = field(default_factory=list)


def _text_overlap(a: str, b: str) -> float:
    """Compute Jaccard similarity on word-level tokens between two texts."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def _subject_predicate_match(a: Claim, b: Claim) -> bool:
    """Return True if both claims share the same subject AND predicate (when present)."""
    if a.subject and b.subject and a.predicate and b.predicate:
        return (
            a.subject.strip().lower() == b.subject.strip().lower()
            and a.predicate.strip().lower() == b.predicate.strip().lower()
        )
    return False


def _pick_survivor(a: Claim, b: Claim) -> tuple[Claim, Claim]:
    """Return (keep, archive) picking the claim with highest confidence.

    Ties are broken by: pinned > newer updated_at > lower id.
    """
    if a.confidence != b.confidence:
        return (a, b) if a.confidence >= b.confidence else (b, a)
    if a.pinned != b.pinned:
        return (a, b) if a.pinned else (b, a)
    if a.updated_at != b.updated_at:
        return (a, b) if a.updated_at >= b.updated_at else (b, a)
    return (a, b) if a.id <= b.id else (b, a)


def find_duplicates(
    claims: list[Claim],
    provider: EmbeddingProvider,
    *,
    threshold: float = 0.92,
    min_text_overlap: float = 0.3,
) -> list[DuplicatePair]:
    """Scan claims and return duplicate pairs above the similarity threshold.

    A pair is flagged as duplicate when:
      1. cosine_similarity(embedding_a, embedding_b) >= threshold, AND
      2. Either subject+predicate match OR word-level Jaccard overlap >= min_text_overlap

    This two-gate approach prevents false positives from hash embeddings while still
    catching true semantic duplicates when a real embedding provider is available.
    """
    if len(claims) < 2:
        return []

    # Pre-compute embeddings
    embeddings: list[list[float]] = []
    for claim in claims:
        embed_text = claim.text
        if claim.subject and claim.predicate:
            embed_text = f"{claim.subject} {claim.predicate} {claim.object_value or ''} {claim.text}"
        embeddings.append(provider.embed(embed_text))

    # Track which claim ids are already marked for archiving to avoid chains
    archived_ids: set[int] = set()
    pairs: list[DuplicatePair] = []

    for i in range(len(claims)):
        if claims[i].id in archived_ids:
            continue
        for j in range(i + 1, len(claims)):
            if claims[j].id in archived_ids:
                continue
            sim = cosine_similarity(embeddings[i], embeddings[j])
            if sim < threshold:
                continue
            # Second gate: text overlap or subject/predicate match
            overlap = _text_overlap(claims[i].text, claims[j].text)
            sp_match = _subject_predicate_match(claims[i], claims[j])
            if not sp_match and overlap < min_text_overlap:
                continue

            keep, archive = _pick_survivor(claims[i], claims[j])
            archived_ids.add(archive.id)
            pairs.append(
                DuplicatePair(
                    keep_id=keep.id,
                    archive_id=archive.id,
                    similarity=round(sim, 4),
                    text_overlap=round(overlap, 4),
                    keep_confidence=keep.confidence,
                    archive_confidence=archive.confidence,
                    keep_text=keep.text,
                    archive_text=archive.text,
                )
            )
    return pairs


def run(
    store,
    *,
    threshold: float = 0.92,
    min_text_overlap: float = 0.3,
    dry_run: bool = False,
    provider: EmbeddingProvider | None = None,
) -> dict:
    """Run the deduplication engine.

    Args:
        store: SQLiteStore (or compatible) instance.
        threshold: Cosine similarity threshold for duplicate detection.
        min_text_overlap: Minimum Jaccard word overlap as fallback gate.
        dry_run: If True, report duplicates without archiving.
        provider: Embedding provider; auto-detected if None.

    Returns:
        Dict with scanned, duplicates_found, claims_archived, and pairs detail.
    """
    if provider is None:
        provider = create_best_provider()

    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    # Fetch all non-archived claims
    active_statuses = ["candidate", "confirmed", "stale", "conflicted"]
    claims = store.list_claims(
        status_in=active_statuses,
        limit=10000,
        include_archived=False,
        include_citations=False,
    )

    logger.info("Dedup: scanning %d claims (threshold=%.2f, dry_run=%s)", len(claims), threshold, dry_run)

    pairs = find_duplicates(
        claims,
        provider,
        threshold=threshold,
        min_text_overlap=min_text_overlap,
    )

    result = DedupResult(
        scanned=len(claims),
        duplicates_found=len(pairs),
        pairs=pairs,
    )

    if not dry_run:
        for pair in pairs:
            try:
                transition_claim(
                    store,
                    claim_id=pair.archive_id,
                    to_status="archived",
                    reason=f"duplicate of claim {pair.keep_id} (similarity={pair.similarity})",
                    event_type="dedup",
                    replaced_by_claim_id=pair.keep_id,
                )
                # Create a link between the surviving and archived claim
                try:
                    store.add_claim_link(pair.keep_id, pair.archive_id, "supersedes")
                except Exception:
                    # Link may already exist or claims may have constraint issues
                    logger.debug("Could not create supersedes link %d -> %d", pair.keep_id, pair.archive_id)
                result.claims_archived += 1
            except Exception as exc:
                logger.warning(
                    "Failed to archive duplicate claim %d: %s", pair.archive_id, exc
                )

        # Record a summary event
        store.record_event(
            claim_id=None,
            event_type="dedup_run",
            details="dedup_completed",
            payload={
                "generated_at": generated_at,
                "threshold": threshold,
                "min_text_overlap": min_text_overlap,
                "dry_run": dry_run,
                "scanned": result.scanned,
                "duplicates_found": result.duplicates_found,
                "claims_archived": result.claims_archived,
            },
        )

    return {
        "scanned": result.scanned,
        "duplicates_found": result.duplicates_found,
        "claims_archived": result.claims_archived,
        "dry_run": dry_run,
        "threshold": threshold,
        "pairs": [
            {
                "keep_id": p.keep_id,
                "archive_id": p.archive_id,
                "similarity": p.similarity,
                "text_overlap": p.text_overlap,
                "keep_confidence": p.keep_confidence,
                "archive_confidence": p.archive_confidence,
                "keep_text": p.keep_text,
                "archive_text": p.archive_text,
            }
            for p in result.pairs
        ],
    }
