"""Compact Summaries — LLM-powered summarization of archived claim clusters.

Finds archived claims that haven't been summarized, clusters them by topic
(subject overlap or embedding similarity), and uses an LLM to produce
higher-level summary claims. Each summary is linked to its source claims
via ``derived_from`` claim links.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from memorymaster.embeddings import EmbeddingProvider, cosine_similarity
from memorymaster.llm_steward import (
    KeyRotator,
    PROVIDERS,
    _call_llm,
    _parse_api_keys,
    _parse_extractions,
)
from memorymaster.models import CitationInput

log = logging.getLogger(__name__)

SUMMARIZE_PROMPT = """You are a memory curator for an AI coding agent system.
Given a group of related archived knowledge claims, produce a single concise
summary that captures the key findings, decisions, and facts.

Rules:
- Combine redundant information into concise statements
- Preserve specific values (IPs, ports, commands, file paths, versions)
- Keep the summary factual and actionable — no filler
- Output ONLY a JSON object with these keys:
  - "summary_text": the consolidated summary (1-4 sentences)
  - "subject": the primary topic/entity these claims are about
  - "predicate": a relationship like "summary_of", "key_findings_for", etc.
  - "object_value": the core conclusion or value (1 sentence max)
  - "confidence": 0.0-1.0 how reliable the combined knowledge is

Claims to summarize:
---
{claims_text}
---

Output JSON object:"""


@dataclass
class CompactSummaryResult:
    """Result of a compact-summaries run."""
    clusters_found: int
    summaries_created: int
    source_claims_summarized: int
    errors: int
    dry_run: bool
    details: list[dict[str, Any]]


def _build_claim_text_block(claims: list[Any]) -> str:
    """Format a list of claims into a text block for the LLM prompt."""
    lines: list[str] = []
    for i, claim in enumerate(claims, 1):
        parts = [f"[{i}]"]
        if claim.subject:
            parts.append(f"Subject: {claim.subject}")
        if claim.predicate:
            parts.append(f"Predicate: {claim.predicate}")
        if claim.object_value:
            parts.append(f"Value: {claim.object_value}")
        if claim.text:
            parts.append(f"Text: {claim.text[:300]}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _cluster_by_subject(claims: list[Any]) -> dict[str, list[Any]]:
    """Group claims by their subject field (simple string matching)."""
    clusters: dict[str, list[Any]] = {}
    for claim in claims:
        key = (claim.subject or "unknown").strip().lower()
        clusters.setdefault(key, []).append(claim)
    return clusters


def _cluster_by_embedding(
    claims: list[Any],
    store: Any,
    provider: EmbeddingProvider,
    similarity_threshold: float = 0.65,
) -> list[list[Any]]:
    """Cluster claims using embedding cosine similarity (greedy merging).

    Falls back to subject-based clustering if embeddings aren't available.
    """
    if not provider.is_semantic:
        # Fall back to subject-based clustering, return as list of lists
        subject_clusters = _cluster_by_subject(claims)
        return list(subject_clusters.values())

    # Compute embeddings for all claims
    claim_embeddings: dict[int, list[float]] = {}
    for claim in claims:
        text = " ".join(
            part for part in [
                claim.text or "",
                claim.subject or "",
                claim.predicate or "",
                claim.object_value or "",
            ] if part
        )
        if text.strip():
            claim_embeddings[claim.id] = provider.embed(text)

    if not claim_embeddings:
        return [claims] if claims else []

    # Greedy clustering: assign each claim to the first cluster it's similar to
    clusters: list[list[Any]] = []
    cluster_centroids: list[list[float]] = []
    claim_by_id = {c.id: c for c in claims}

    for claim_id, embedding in claim_embeddings.items():
        assigned = False
        for idx, centroid in enumerate(cluster_centroids):
            if cosine_similarity(embedding, centroid) >= similarity_threshold:
                clusters[idx].append(claim_by_id[claim_id])
                # Update centroid as running average
                n = len(clusters[idx])
                cluster_centroids[idx] = [
                    (c * (n - 1) + e) / n
                    for c, e in zip(centroid, embedding)
                ]
                assigned = True
                break
        if not assigned:
            clusters.append([claim_by_id[claim_id]])
            cluster_centroids.append(list(embedding))

    # Add claims without embeddings to existing clusters by subject, or as singletons
    embedded_ids = set(claim_embeddings.keys())
    for claim in claims:
        if claim.id not in embedded_ids:
            placed = False
            for cluster in clusters:
                if any(
                    c.subject and claim.subject
                    and c.subject.strip().lower() == claim.subject.strip().lower()
                    for c in cluster
                ):
                    cluster.append(claim)
                    placed = True
                    break
            if not placed:
                clusters.append([claim])

    return clusters


def _get_unsummarized_archived_claims(store: Any, limit: int = 500) -> list[Any]:
    """Fetch archived claims that haven't already been summarized.

    A claim is considered "summarized" if it's the target of a ``derived_from``
    link from another claim.
    """
    all_archived = store.list_claims(
        status="archived",
        limit=limit,
        include_archived=True,
    )
    if not all_archived:
        return []

    # Filter out claims that are already targets of derived_from links
    unsummarized = []
    for claim in all_archived:
        links = store.get_claim_links(claim.id)
        is_summarized = any(
            link.target_id == claim.id and link.link_type == "derived_from"
            for link in links
        )
        if not is_summarized:
            unsummarized.append(claim)

    return unsummarized


def run(
    store: Any,
    *,
    provider: str = "gemini",
    api_key: str = "",
    model: str = "",
    base_url: str = "",
    min_cluster: int = 3,
    max_cluster: int = 20,
    similarity_threshold: float = 0.65,
    dry_run: bool = False,
    limit: int = 500,
    api_keys: list[str] | None = None,
    cooldown_seconds: float = 60.0,
    embedding_provider: EmbeddingProvider | None = None,
) -> CompactSummaryResult:
    """Run LLM-powered compaction summaries on archived claims.

    Args:
        store: The storage backend (SQLiteStore or PostgresStore).
        provider: LLM provider name.
        api_key: Single API key.
        model: Model name (uses provider default if empty).
        base_url: Custom API base URL.
        min_cluster: Minimum claims per cluster to summarize.
        max_cluster: Maximum claims per cluster (splits larger groups).
        similarity_threshold: Cosine similarity threshold for clustering.
        dry_run: If True, don't write to DB.
        limit: Maximum archived claims to consider.
        api_keys: Optional list of API keys for rotation.
        cooldown_seconds: Cooldown for rate-limited keys.
        embedding_provider: Provider for computing embeddings. If None,
            uses subject-based clustering.

    Returns:
        CompactSummaryResult with stats and details.
    """
    cfg = PROVIDERS.get(provider, PROVIDERS["custom"])
    if not model:
        model = cfg["default_model"]

    # Build key rotator
    effective_keys = api_keys if api_keys else [api_key] if api_key else [""]
    key_rotator: KeyRotator | None = None
    if len(effective_keys) > 1:
        key_rotator = KeyRotator(keys=effective_keys, cooldown_seconds=cooldown_seconds)
        log.info("Key rotation enabled with %d keys", key_rotator.key_count)

    # 1. Find unsummarized archived claims
    archived_claims = _get_unsummarized_archived_claims(store, limit=limit)
    if not archived_claims:
        log.info("No unsummarized archived claims found")
        return CompactSummaryResult(
            clusters_found=0,
            summaries_created=0,
            source_claims_summarized=0,
            errors=0,
            dry_run=dry_run,
            details=[],
        )

    log.info("Found %d unsummarized archived claims", len(archived_claims))

    # 2. Cluster claims
    if embedding_provider is not None and embedding_provider.is_semantic:
        clusters = _cluster_by_embedding(
            archived_claims, store, embedding_provider,
            similarity_threshold=similarity_threshold,
        )
    else:
        subject_clusters = _cluster_by_subject(archived_claims)
        clusters = list(subject_clusters.values())

    # Filter to clusters meeting min_cluster threshold
    eligible_clusters = [c for c in clusters if len(c) >= min_cluster]
    log.info(
        "Clustered into %d groups, %d eligible (>= %d claims)",
        len(clusters), len(eligible_clusters), min_cluster,
    )

    # 3. For each eligible cluster, call LLM to summarize
    summaries_created = 0
    source_claims_summarized = 0
    errors = 0
    details: list[dict[str, Any]] = []

    for cluster in eligible_clusters:
        # Split oversized clusters
        sub_clusters = [
            cluster[i:i + max_cluster]
            for i in range(0, len(cluster), max_cluster)
        ]
        for sub_cluster in sub_clusters:
            if len(sub_cluster) < min_cluster:
                continue

            claims_text = _build_claim_text_block(sub_cluster)
            prompt = SUMMARIZE_PROMPT.replace("{claims_text}", claims_text)

            detail: dict[str, Any] = {
                "source_claim_ids": [c.id for c in sub_cluster],
                "cluster_size": len(sub_cluster),
                "subject_hint": sub_cluster[0].subject or "unknown",
            }

            if dry_run:
                detail["action"] = "would_summarize"
                details.append(detail)
                summaries_created += 1
                source_claims_summarized += len(sub_cluster)
                continue

            try:
                raw_response = _call_llm(
                    provider, api_key, model, prompt, base_url,
                    key_rotator=key_rotator,
                )

                # Parse the LLM response
                parsed = _parse_extractions(raw_response)
                if not parsed:
                    log.warning(
                        "LLM returned empty/unparseable response for cluster with subject '%s'",
                        sub_cluster[0].subject,
                    )
                    detail["action"] = "llm_empty_response"
                    detail["raw_response"] = raw_response[:200]
                    errors += 1
                    details.append(detail)
                    continue

                extraction = parsed[0] if isinstance(parsed, list) else parsed
                summary_text = extraction.get("summary_text", "")
                subject = extraction.get("subject", sub_cluster[0].subject or "unknown")
                base_predicate = extraction.get("predicate", "summary_of")
                object_value = extraction.get("object_value", "")

                # Make predicate unique per cluster to avoid confirmed tuple collisions
                cluster_ids_str = ",".join(str(c.id) for c in sub_cluster)
                cluster_hash = hashlib.sha1(cluster_ids_str.encode()).hexdigest()[:8]
                predicate = f"{base_predicate}:{cluster_hash}"
                confidence = min(1.0, max(0.0, float(extraction.get("confidence", 0.85))))

                if not summary_text:
                    log.warning("LLM returned empty summary_text for cluster")
                    detail["action"] = "empty_summary"
                    errors += 1
                    details.append(detail)
                    continue

                # Create the summary claim
                summary_claim = store.create_claim(
                    text=summary_text,
                    citations=[CitationInput(
                        source="compact-summaries",
                        locator=f"cluster:{len(sub_cluster)}_claims",
                        excerpt=f"Summarized from claim IDs: {[c.id for c in sub_cluster]}",
                    )],
                    claim_type="summary",
                    subject=subject,
                    predicate=predicate,
                    object_value=object_value,
                    scope=sub_cluster[0].scope or "project",
                    confidence=confidence,
                )

                # Transition summary claim to confirmed status
                from memorymaster.lifecycle import transition_claim
                transition_claim(
                    store,
                    claim_id=summary_claim.id,
                    to_status="confirmed",
                    reason="compact-summary: LLM-generated summary of archived claims",
                    event_type="compactor",
                )

                # Create derived_from links from summary -> each source claim
                links_created = 0
                for source_claim in sub_cluster:
                    try:
                        store.add_claim_link(
                            source_id=summary_claim.id,
                            target_id=source_claim.id,
                            link_type="derived_from",
                        )
                        links_created += 1
                    except ValueError as e:
                        log.debug("Could not create link: %s", e)

                # Record event
                store.record_event(
                    claim_id=summary_claim.id,
                    event_type="compactor",
                    details="compact_summary_created",
                    payload={
                        "source_claim_ids": [c.id for c in sub_cluster],
                        "cluster_size": len(sub_cluster),
                        "links_created": links_created,
                    },
                )

                detail["action"] = "summarized"
                detail["summary_claim_id"] = summary_claim.id
                detail["summary_text"] = summary_text[:200]
                detail["links_created"] = links_created
                details.append(detail)

                summaries_created += 1
                source_claims_summarized += len(sub_cluster)
                log.info(
                    "Created summary claim #%d from %d archived claims (subject: %s)",
                    summary_claim.id, len(sub_cluster), subject,
                )

            except Exception as e:
                log.warning("LLM error for cluster (subject: %s): %s", sub_cluster[0].subject, e)
                detail["action"] = "error"
                detail["error"] = str(e)
                errors += 1
                details.append(detail)

    return CompactSummaryResult(
        clusters_found=len(eligible_clusters),
        summaries_created=summaries_created,
        source_claims_summarized=source_claims_summarized,
        errors=errors,
        dry_run=dry_run,
        details=details,
    )
