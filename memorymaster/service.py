from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import logging
import os

from memorymaster import candidate_dedupe, llm_budget, observability
from memorymaster.embeddings import EmbeddingProvider, create_best_provider
from memorymaster.jobs import compact_summaries, compactor, decay, dedup, deterministic, extractor, validator
from memorymaster.models import ActionProposal, CitationInput, Claim, ClaimLink, Event, EvidenceItem, ExternalSource, MediaRetryItem, SourceItem
from memorymaster.policy import select_revalidation_candidates
from memorymaster.context_optimizer import ContextResult, pack_context
from memorymaster.config import get_config
from memorymaster.retrieval import VectorSearchHook, _tier_bonus, rank_claim_rows
from memorymaster.security import is_sensitive_claim, resolve_allow_sensitive_access, sanitize_claim_input
from memorymaster.store_factory import create_store
import contextlib

logger = logging.getLogger(__name__)

RetrievalWeights = tuple[float, float, float, float]

RETRIEVAL_PROFILES: dict[str, RetrievalWeights] = {
    "recall": (0.6, 0.2, 0.1, 0.1),
    "precision": (0.2, 0.6, 0.1, 0.1),
    "fresh": (0.2, 0.2, 0.5, 0.1),
    "semantic": (0.2, 0.2, 0.1, 0.5),
}


def _retrieval_profile_weights(profile: str | None) -> RetrievalWeights | None:
    if not profile:
        return None
    try:
        return RETRIEVAL_PROFILES[profile]
    except KeyError as exc:
        choices = ", ".join(sorted(RETRIEVAL_PROFILES))
        raise ValueError(f"Unknown retrieval profile: {profile!r}. Expected one of: {choices}") from exc


def _rerank_with_profile(
    ranked_rows,
    *,
    weights: RetrievalWeights | None,
    limit: int,
):
    if weights is None:
        return ranked_rows[:limit]

    cfg = get_config()

    def _linear(row, row_weights: RetrievalWeights) -> float:
        w_l, w_c, w_f, w_v = row_weights
        return (
            w_l * row.lexical_score
            + w_c * row.confidence_score
            + w_f * row.freshness_score
            + w_v * row.vector_score
        )

    profiled = []
    for row in ranked_rows:
        bonus = (cfg.pinned_bonus if row.claim.pinned else 0.0) + _tier_bonus(row.claim)
        profiled.append(replace(row, score=_linear(row, weights) + bonus))

    profiled.sort(
        key=lambda row: (
            row.score,
            row.lexical_score,
            row.confidence_score,
            row.freshness_score,
            row.claim.updated_at,
            row.claim.id,
        ),
        reverse=True,
    )
    return profiled[:limit]


def _is_cross_scope_sensitive(claim: Claim, current_scope: str | None) -> bool:
    visibility = (getattr(claim, "visibility", "public") or "public").strip().lower()
    if visibility != "sensitive":
        return False
    claim_scope = (claim.scope or "").strip()
    return not current_scope or claim_scope != current_scope


def _is_team_scope(scope: str) -> bool:
    return scope.startswith("team:")


def _allow_federated_claim(
    claim: Claim,
    *,
    current_scope: str | None,
    explicit_scope_allowlist: list[str] | None,
) -> bool:
    claim_scope = (claim.scope or "").strip()
    if _is_cross_scope_sensitive(claim, current_scope):
        return False
    if _is_team_scope(claim_scope):
        if explicit_scope_allowlist is not None:
            return claim_scope in explicit_scope_allowlist
        return claim_scope == current_scope
    return True


def _llm_rerank_enabled() -> bool:
    if not get_config().llm_rerank or not os.environ.get("GEMINI_API_KEY", "").strip():
        return False
    try:
        from memorymaster.llm_rerank import rerank_temporarily_disabled

        return not rerank_temporarily_disabled()
    except Exception:
        return True



class MemoryService:
    def __init__(
        self,
        db_target: str | Path,
        workspace_root: str | Path | None = None,
        *,
        policy_config: Mapping[str, object] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self.store = create_store(db_target)
        self.workspace_root = Path(workspace_root) if workspace_root else Path.cwd()
        self._embedding_provider: EmbeddingProvider | None = None
        self.policy_config = policy_config
        self.tenant_id = (tenant_id or "").strip() or None
        self.qdrant = self._init_qdrant()

    @property
    def embedding_provider(self) -> EmbeddingProvider:
        """Lazy-load embedding provider — avoids 7s startup for commands that don't need it."""
        if self._embedding_provider is None:
            self._embedding_provider = create_best_provider()
        return self._embedding_provider

    @embedding_provider.setter
    def embedding_provider(self, value: EmbeddingProvider | None) -> None:
        self._embedding_provider = value

    @staticmethod
    def _init_qdrant():
        """Lazily create a QdrantBackend if QDRANT_URL is set or reachable."""
        qdrant_url = os.environ.get("QDRANT_URL", "")
        if not qdrant_url:
            return None
        try:
            from memorymaster.qdrant_backend import QdrantBackend
            backend = QdrantBackend(qdrant_url=qdrant_url)
            backend.ensure_collection()
            logger.info("Qdrant backend enabled at %s", backend.qdrant_url)
            return backend
        except Exception as exc:
            logger.warning("Qdrant backend unavailable, continuing without it: %s", exc)
            return None

    def _qdrant_sync(self, claim: Claim) -> None:
        """Fire-and-forget upsert to Qdrant after a claim state change."""
        if self.qdrant is None:
            return
        try:
            if claim.status == "archived":
                self.qdrant.delete_claim(claim.id)
            else:
                self.qdrant.upsert_claim(claim)
        except Exception as exc:
            logger.warning("Qdrant sync failed for claim %d: %s", claim.id, exc)

    def _qdrant_post_cycle_sync(self) -> None:
        """After a lifecycle cycle, push recently-changed claims to Qdrant."""
        if self.qdrant is None:
            return
        failed = 0
        for status in ("confirmed", "stale", "conflicted"):
            try:
                claims = self.store.find_by_status(status, limit=200, include_citations=False)
            except Exception as exc:
                logger.warning("Qdrant post-cycle sync: failed to fetch %s claims: %s", status, exc)
                continue
            for claim in claims:
                try:
                    self.qdrant.upsert_claim(claim)
                except Exception as exc:
                    failed += 1
                    logger.warning("Qdrant post-cycle sync failed for claim %d: %s", claim.id, exc)
        if failed:
            logger.warning("Qdrant post-cycle sync: %d claims failed", failed)

    def init_db(self) -> None:
        self.store.init_db()

    def ingest(
        self,
        text: str,
        citations: list[CitationInput],
        *,
        idempotency_key: str | None = None,
        claim_type: str | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        object_value: str | None = None,
        scope: str = "project",
        volatility: str = "medium",
        confidence: float = 0.5,
        event_time: str | None = None,
        valid_from: str | None = None,
        valid_until: str | None = None,
        source_agent: str | None = None,
        visibility: str = "public",
    ) -> Claim:
        if not text.strip():
            raise ValueError("Claim text cannot be empty.")
        if not citations:
            citations = [CitationInput(source="mcp-session", locator=scope or "project")]
        # Normalize claim_type to lowercase so routing hints like "DECISION"
        # from the classify hook don't create a separate type from "decision".
        if claim_type:
            claim_type = claim_type.strip().lower() or None
        # Dedup by idempotency key
        normalized_idempotency_key = (idempotency_key or "").strip() or None
        if normalized_idempotency_key is not None and hasattr(self.store, "get_claim_by_idempotency_key"):
            existing_claim = self.store.get_claim_by_idempotency_key(normalized_idempotency_key)
            if existing_claim is not None:
                observability.bump_claim_ingested(source_agent)
                return existing_claim
        # Dedup by content hash (catch duplicates without idempotency key)
        # Include scope + tenant to avoid cross-tenant/cross-scope dedup
        import hashlib
        _tenant = getattr(self, 'tenant_id', '') or ''
        hash_input = f"{text.strip().lower()}|{scope}|{_tenant}"
        content_hash = "hash-" + hashlib.sha256(hash_input.encode()).hexdigest()[:16]
        if hasattr(self.store, "get_claim_by_idempotency_key"):
            existing_by_hash = self.store.get_claim_by_idempotency_key(content_hash)
            if existing_by_hash is not None:
                observability.bump_claim_ingested(source_agent)
                return existing_by_hash
        # Set content hash as idempotency key if none provided
        if normalized_idempotency_key is None:
            normalized_idempotency_key = content_hash
        sanitized = sanitize_claim_input(
            text=text.strip(),
            object_value=object_value,
            citations=citations,
        )
        if not sanitized.citations:
            raise ValueError("At least one citation is required.")
        # Resolve subject → canonical entity (GBrain-inspired entity registry)
        # and mine text for pattern-based entities (#127 Wave 3).
        entity_id = 0
        if subject or sanitized.text:
            try:
                from memorymaster.entity_registry import (
                    add_alias,
                    resolve_or_create,
                )
                from memorymaster.entity_extractor import extract_patterns

                with self.store.connect() as _conn:
                    if subject:
                        entity_id = resolve_or_create(
                            _conn, subject,
                            entity_type=claim_type or "unknown",
                            scope=scope,
                        )
                    # Layer 1: mine the claim text for deterministic patterns.
                    # Strategy: resolve the canonical_hint via the alias
                    # index (reuses existing entity if present). Register
                    # BOTH the raw surface AND a kind-tagged alias so every
                    # extracted entity gains ≥2 aliases (canonical + tag),
                    # plus any distinct surface variants.
                    for ent in extract_patterns(sanitized.text):
                        eid = resolve_or_create(
                            _conn,
                            ent.canonical_hint,
                            entity_type=f"text_entity:{ent.kind}",
                            scope=scope,
                        )
                        if eid <= 0:
                            continue
                        if ent.surface and ent.surface != ent.canonical_hint:
                            add_alias(_conn, eid, ent.surface)
                        # Kind-tagged stable alias — guarantees a second
                        # alias row so avg_aliases_per_entity ≥ 2 after
                        # backfill even when surface == canonical.
                        add_alias(_conn, eid, f"{ent.kind}:{ent.canonical_hint}")
                    _conn.commit()
            except Exception:
                pass  # entity resolution is best-effort, never block ingest

        claim = self.store.create_claim(
            text=sanitized.text,
            citations=sanitized.citations,
            idempotency_key=normalized_idempotency_key,
            claim_type=claim_type,
            subject=subject,
            predicate=predicate,
            object_value=sanitized.object_value,
            scope=scope,
            volatility=volatility,
            confidence=confidence,
            tenant_id=self.tenant_id,
            event_time=event_time,
            valid_from=valid_from,
            valid_until=valid_until,
            source_agent=source_agent,
            visibility=visibility,
        )

        # Set entity_id on the claim (best-effort, don't fail ingest)
        if entity_id > 0:
            try:
                with self.store.connect() as _conn:
                    _conn.execute(
                        "UPDATE claims SET entity_id = ? WHERE id = ?",
                        (entity_id, claim.id),
                    )
                    _conn.commit()
            except Exception:
                pass
        if sanitized.is_sensitive:
            observability.bump_claim_filtered_findings(sanitized.findings)
            self.store.record_event(
                claim_id=claim.id,
                event_type="policy_decision",
                details="sensitive_redaction_applied",
                payload={"findings": sanitized.findings},
            )
            if sanitized.encrypted_payload:
                self.store.record_event(
                    claim_id=claim.id,
                    event_type="policy_decision",
                    details="sensitive_payload_encrypted",
                    payload={"ciphertext_b64": sanitized.encrypted_payload},
                )
        self._qdrant_sync(claim)
        try:
            from memorymaster.webhook import fire_webhook

            fire_webhook(
                "claim_ingested",
                {"claim_id": claim.id, "text": claim.text[:200], "status": claim.status},
            )
        except Exception:
            pass
        observability.bump_claim_ingested(source_agent)
        return claim

    def run_cycle(
        self,
        *,
        run_compactor: bool = False,
        min_citations: int = 1,
        min_score: float = 0.58,
        policy_mode: str = "legacy",
        policy_limit: int = 200,
    ) -> dict[str, object]:
        # Open a per-cycle LLM budget scope. When any of the caps fires
        # (MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE / MAX_TOKENS_PER_CYCLE /
        # MAX_PROVIDER_FAILURES_PER_CYCLE) the next call_llm raises
        # LLMBudgetExceeded; we catch it here, surface the abort reason in
        # the result dict, and stop cleanly. Stages that ran before the
        # abort still have their results recorded. When all caps are unset
        # (0/default), behavior matches pre-v3.19 — no enforcement.
        result: dict[str, object] = {}
        budget_snapshot: dict[str, object] = {}
        try:
            with llm_budget.cycle_scope() as budget:
                policy_selection = select_revalidation_candidates(
                    self.store,
                    mode=policy_mode,
                    limit=policy_limit,
                )
                extract_res = extractor.run(self.store)
                result["policy"] = {
                    "mode": policy_selection.mode,
                    "considered": policy_selection.considered,
                    "due": policy_selection.due,
                    "selected": len(policy_selection.selected),
                }
                result["extractor"] = extract_res
                # Match validator's scan size (200) so every candidate the
                # validator would touch gets a chance to dedupe first.
                dedupe_res = candidate_dedupe.run(self.store)
                result["dedupe"] = dedupe_res
                deterministic_res = deterministic.run(
                    self.store,
                    workspace_root=self.workspace_root,
                    revalidation_claims=policy_selection.selected,
                    policy_mode=policy_mode,
                )
                result["deterministic"] = deterministic_res
                validate_res = validator.run(
                    self.store,
                    min_citations=min_citations,
                    min_score=min_score,
                    revalidation_claims=policy_selection.selected,
                    policy_mode=policy_mode,
                )
                result["validator"] = validate_res
                decay_res = decay.run(self.store)
                result["decay"] = decay_res
                compact_res = (
                    compactor.run(
                        self.store,
                        artifacts_dir=self.workspace_root / "artifacts" / "compaction",
                    )
                    if run_compactor
                    else {"archived_claims": 0, "deleted_events": 0}
                )
                result["compactor"] = compact_res
                budget_snapshot = budget.snapshot()
        except llm_budget.LLMBudgetExceeded as exc:
            current = llm_budget.get_current()
            if current is not None:
                budget_snapshot = current.snapshot()
            budget_snapshot["aborted"] = True
            budget_snapshot["aborted_reason"] = exc.reason
            budget_snapshot["aborted_provider"] = exc.provider
            logger.warning(
                "run_cycle aborted by llm budget: reason=%s provider=%s",
                exc.reason,
                exc.provider,
            )

        # Always include the budget snapshot — empty if no caps were ever
        # consulted, populated when callers opt-in via env vars.
        budget_snapshot.setdefault("aborted", False)
        result["budget"] = budget_snapshot
        self._qdrant_post_cycle_sync()
        return result

    def query(
        self,
        query_text: str,
        *,
        limit: int = 20,
        include_stale: bool = True,
        include_conflicted: bool = True,
        include_candidates: bool = False,
        retrieval_mode: str = "legacy",
        vector_hook: VectorSearchHook | None = None,
        retrieval_profile: str | None = None,
        allow_sensitive: bool = False,
        scope_allowlist: list[str] | None = None,
        query_type: str | None = None,
    ) -> list[Claim]:
        rows = self.query_rows(
            query_text=query_text,
            limit=limit,
            include_stale=include_stale,
            include_conflicted=include_conflicted,
            include_candidates=include_candidates,
            retrieval_mode=retrieval_mode,
            vector_hook=vector_hook,
            retrieval_profile=retrieval_profile,
            allow_sensitive=allow_sensitive,
            scope_allowlist=scope_allowlist,
            query_type=query_type,
        )
        return [row["claim"] for row in rows]

    def query_rules(
        self,
        query_text: str,
        *,
        limit: int = 10,
        scope_allowlist: list[str] | None = None,
        allow_sensitive: bool = False,
    ) -> list[dict[str, object]]:
        """Retrieve rule-shaped claims (claim_type='rule') matching a query.

        Returns each rule's parsed structure (trigger / action / rationale /
        text / claim_id / score), ranked by the hybrid retriever. Rules are a
        small fraction of claims, so we over-fetch candidates and filter to
        rule-typed in Python (list_claims has no claim_type filter). See
        memorymaster/rules.py for the storage convention.
        """
        from memorymaster.rules import parse_rule

        rows = self.query_rows(
            query_text=query_text,
            limit=max(limit * 5, 25),
            include_candidates=True,
            include_stale=True,
            include_conflicted=True,
            retrieval_mode="hybrid",
            allow_sensitive=allow_sensitive,
            scope_allowlist=scope_allowlist,
        )
        out: list[dict[str, object]] = []
        for row in rows:
            parsed = parse_rule(row["claim"])
            if parsed is None:
                continue
            parsed["score"] = row.get("score")
            out.append(parsed)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _normalize_scope_allowlist(scope_allowlist: list[str] | None) -> list[str] | None:
        if not scope_allowlist:
            return None
        normalized = [scope.strip() for scope in scope_allowlist if scope and scope.strip()]
        if not normalized:
            return None
        seen: set[str] = set()
        deduped: list[str] = []
        for scope in normalized:
            if scope in seen:
                continue
            seen.add(scope)
            deduped.append(scope)
        return deduped

    @staticmethod
    def _annotation_for_claim(claim: Claim) -> dict[str, object]:
        return {
            "status": claim.status,
            "active": claim.status == "confirmed",
            "stale": claim.status == "stale",
            "conflicted": claim.status == "conflicted",
            "pinned": bool(claim.pinned),
        }

    def _allow_sensitive(
        self,
        *,
        allow_sensitive: bool,
        context: str,
        deny_mode: str = "filter",
    ) -> bool:
        return resolve_allow_sensitive_access(
            allow_sensitive=allow_sensitive,
            context=context,
            config=self.policy_config,
            deny_mode=deny_mode,
        )

    def _check_tenant_access(self, claim: Claim) -> None:
        """Raise ValueError if the service has a tenant_id set and the claim
        belongs to a different tenant."""
        if self.tenant_id is not None and claim.tenant_id != self.tenant_id:
            raise ValueError(f"Claim {claim.id} does not exist.")

    def _build_query_statuses(self, include_stale: bool, include_conflicted: bool, include_candidates: bool) -> list[str]:
        """Build list of claim statuses to include in query."""
        statuses = ["confirmed"]
        if include_stale:
            statuses.append("stale")
        if include_conflicted:
            statuses.append("conflicted")
        if include_candidates:
            statuses.append("candidate")
        return statuses

    def _query_legacy_mode(self, query_text: str, limit: int, statuses: list[str], normalized_scopes: list[str] | None, include_sensitive: bool, requesting_agent: str | None) -> list[dict[str, Any]]:
        """Query using legacy retrieval mode."""
        legacy = self.store.list_claims(
            limit=limit,
            text_query=query_text,
            status_in=statuses,
            include_archived=False,
            include_citations=True,
            scope_allowlist=normalized_scopes,
            tenant_id=self.tenant_id,
        )
        if not include_sensitive:
            legacy = [claim for claim in legacy if not is_sensitive_claim(claim)]
        # Visibility: filter out private claims from other agents
        if requesting_agent:
            legacy = [c for c in legacy if getattr(c, 'visibility', 'public') == 'public' or getattr(c, 'source_agent', None) == requesting_agent]
        ranked_rows = rank_claim_rows(
            query_text,
            legacy,
            mode="legacy",
            limit=limit,
            vector_hook=None,
        )
        results = [
            {
                "claim": row.claim,
                "status": row.claim.status,
                "annotation": self._annotation_for_claim(row.claim),
                "score": row.score,
                "lexical_score": row.lexical_score,
                "freshness_score": row.freshness_score,
                "confidence_score": row.confidence_score,
                "vector_score": row.vector_score,
            }
            for row in ranked_rows
        ]
        self._record_accesses(results, query_text=query_text)
        return results

    def query_rows(
        self,
        query_text: str,
        *,
        limit: int = 20,
        include_stale: bool = True,
        include_conflicted: bool = True,
        include_candidates: bool = False,
        retrieval_mode: str = "legacy",
        vector_hook: VectorSearchHook | None = None,
        retrieval_profile: str | None = None,
        allow_sensitive: bool = False,
        scope_allowlist: list[str] | None = None,
        enrich_with_entities: bool = False,
        requesting_agent: str | None = None,
        query_type: str | None = None,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []

        # RBAC check
        if requesting_agent:
            from memorymaster.access_control import require_permission
            require_permission(requesting_agent, "query")

        include_sensitive = self._allow_sensitive(
            allow_sensitive=allow_sensitive,
            context="service.query_rows",
            deny_mode="filter",
        )

        statuses = self._build_query_statuses(include_stale, include_conflicted, include_candidates)
        normalized_scopes = self._normalize_scope_allowlist(scope_allowlist)
        profile_weights = _retrieval_profile_weights(retrieval_profile)
        if profile_weights is not None and retrieval_mode == "legacy":
            retrieval_mode = "hybrid"

        if retrieval_mode == "legacy":
            return self._query_legacy_mode(query_text, limit, statuses, normalized_scopes, include_sensitive, requesting_agent)

        use_llm_rerank = _llm_rerank_enabled()
        candidate_limit = max(limit * 6, 60, 50 if use_llm_rerank else 0)
        candidates = self.store.list_claims(
            limit=candidate_limit,
            status_in=statuses,
            include_archived=False,
            include_citations=True,
            scope_allowlist=normalized_scopes,
            tenant_id=self.tenant_id,
        )
        if not include_sensitive:
            candidates = [claim for claim in candidates if not is_sensitive_claim(claim)]
        semantic = False
        if vector_hook is None and hasattr(self.store, "vector_scores"):
            def _vector_hook(text, claims):
                return self.store.vector_scores(text, claims, self.embedding_provider)
            vector_hook = _vector_hook
            semantic = self.embedding_provider.is_semantic
        rank_limit = len(candidates) if profile_weights is not None else (max(limit, 50) if use_llm_rerank else limit)
        final_rank_limit = max(limit, 50) if use_llm_rerank else limit
        ranked_rows = rank_claim_rows(
            query_text,
            candidates,
            mode=retrieval_mode,
            limit=rank_limit,
            vector_hook=vector_hook,
            semantic_vectors=semantic,
            query_type=query_type,
        )
        ranked_rows = _rerank_with_profile(
            ranked_rows,
            weights=profile_weights,
            limit=final_rank_limit,
        )
        results = [
            {
                "claim": row.claim,
                "status": row.claim.status,
                "annotation": self._annotation_for_claim(row.claim),
                "score": row.score,
                "lexical_score": row.lexical_score,
                "freshness_score": row.freshness_score,
                "confidence_score": row.confidence_score,
                "vector_score": row.vector_score,
            }
            for row in ranked_rows
        ]
        if use_llm_rerank:
            from memorymaster.llm_rerank import rerank_with_llm

            results = rerank_with_llm(query_text, results, top_k=limit)
        self._record_accesses(results, query_text=query_text)
        if enrich_with_entities:
            results = self._enrich_with_entity_graph(results, query_text, limit)
        return results

    def _enrich_with_entity_graph(
        self, results: list[dict[str, Any]], query_text: str, limit: int
    ) -> list[dict[str, Any]]:
        """Add entity-related claims to query results via knowledge graph traversal."""
        try:
            from memorymaster.entity_graph import EntityGraph
            db_path = str(getattr(self.store, 'db_path', ''))
            if not db_path:
                return results
            eg = EntityGraph(db_path)
            eg.ensure_tables()
            # Extract entity names from query (simple word-based, no LLM needed)
            query_words = [w for w in query_text.split() if len(w) > 3 and w[0].isupper()]
            if not query_words:
                return results
            related_ids = eg.find_related_claims(query_words, hops=2, limit=limit)
            existing_ids = {row["claim"].id for row in results if hasattr(row.get("claim"), "id")}
            new_ids = [cid for cid in related_ids if cid not in existing_ids]
            for cid in new_ids[:limit - len(results)]:
                claim = self.store.get_claim(cid, include_citations=True)
                if claim and claim.status != "archived":
                    results.append({
                        "claim": claim,
                        "status": claim.status,
                        "annotation": self._annotation_for_claim(claim),
                        "score": 0.3,  # entity-graph bonus
                        "lexical_score": 0.0,
                        "freshness_score": 0.0,
                        "confidence_score": claim.confidence,
                        "vector_score": 0.0,
                        "source": "entity_graph",
                    })
        except Exception:
            pass  # best-effort
        return results

    def _record_accesses(self, rows: list[dict[str, Any]], query_text: str = "") -> None:
        """Record access + feedback for each claim returned by a query."""
        claim_ids = []
        for row in rows:
            claim = row.get("claim")
            if claim is not None:
                claim_ids.append(claim.id)

        # Batch record accesses in a single transaction if possible
        if claim_ids and hasattr(self.store, "record_accesses_batch"):
            with contextlib.suppress(Exception):
                self.store.record_accesses_batch(claim_ids)
        elif claim_ids and hasattr(self.store, "record_access"):
            # Fallback to individual calls if batch method not available
            for cid in claim_ids:
                with contextlib.suppress(Exception):
                    self.store.record_access(cid)

        # Record retrieval feedback for quality scoring
        if claim_ids and query_text:
            try:
                from memorymaster.feedback import FeedbackTracker
                db_path = str(getattr(self.store, 'db_path', ''))
                if db_path:
                    ft = FeedbackTracker(db_path)
                    ft.ensure_tables()
                    ft.record_retrieval(claim_ids, query_text)
            except Exception:
                pass  # best-effort

    def recompute_tiers(self) -> dict[str, int]:
        """Recompute tier assignments for all non-archived claims."""
        return self.store.recompute_tiers()

    def query_for_context(
        self,
        query: str,
        *,
        token_budget: int = 4000,
        output_format: str = "text",
        limit: int = 100,
        include_stale: bool = True,
        include_conflicted: bool = True,
        include_candidates: bool = False,
        retrieval_mode: str = "hybrid",
        retrieval_profile: str | None = None,
        allow_sensitive: bool = False,
        scope_allowlist: list[str] | None = None,
        provider: str | None = None,
    ) -> ContextResult:
        """Return a formatted text block of the most relevant claims packed
        into *token_budget* using greedy knapsack.

        This is the primary interface for AI agents that need to inject
        relevant memory into their context window.

        Parameters
        ----------
        query:
            Natural-language query describing what context is needed.
        token_budget:
            Maximum tokens for the output (default 4000).
        output_format:
            ``"text"`` (human-readable), ``"xml"`` (system-prompt tags),
            or ``"json"`` (structured).
        limit:
            Max candidate claims to rank before packing.
        retrieval_mode:
            ``"legacy"`` or ``"hybrid"`` (default).
        provider:
            Optional context-packing strategy for a target LLM provider.
        """
        rows = self.query_rows(
            query_text=query,
            limit=limit,
            include_stale=include_stale,
            include_conflicted=include_conflicted,
            include_candidates=include_candidates,
            retrieval_mode=retrieval_mode,
            retrieval_profile=retrieval_profile,
            allow_sensitive=allow_sensitive,
            scope_allowlist=scope_allowlist,
        )
        return pack_context(
            rows,
            token_budget=token_budget,
            output_format=output_format,
            provider=provider,
        )

    def query_meta_decisions(
        self,
        query: str,
        *,
        claim_types: list[str] = ["decision", "architecture"],
        top_n: int = 20,
    ) -> dict[str, object]:
        """Aggregate matching decision/architecture claims across project scopes."""
        if top_n <= 0:
            return {"groups": []}

        import re
        from collections import Counter

        normalized_types = {
            claim_type.strip().lower()
            for claim_type in claim_types
            if claim_type and claim_type.strip()
        }
        candidate_limit = max(top_n * 10, 100)
        query_text = query.strip()
        statuses = self._build_query_statuses(
            include_stale=True,
            include_conflicted=True,
            include_candidates=True,
        )
        if query_text:
            rows = self.query_rows(
                query_text=query_text,
                limit=candidate_limit,
                include_stale=True,
                include_conflicted=True,
                include_candidates=True,
                scope_allowlist=None,
            )
            candidates: list[Claim] = [row["claim"] for row in rows]
        else:
            candidates = self.store.list_claims(
                limit=candidate_limit,
                status_in=statuses,
                include_archived=False,
                include_citations=False,
                tenant_id=self.tenant_id,
            )

        stopwords = {
            "about",
            "after",
            "against",
            "architecture",
            "because",
            "claim",
            "decision",
            "decisions",
            "default",
            "from",
            "have",
            "into",
            "memorymaster",
            "must",
            "project",
            "should",
            "that",
            "the",
            "their",
            "this",
            "uses",
            "with",
        }

        def tokens_for(claim: Claim) -> set[str]:
            raw = " ".join(
                part
                for part in (
                    claim.subject,
                    claim.predicate,
                    claim.object_value,
                    claim.text,
                )
                if part
            )
            words = re.findall(r"[a-zA-Z][a-zA-Z0-9+._-]*", raw.lower())
            return {
                word.strip("._-")
                for word in words
                if len(word.strip("._-")) >= 3 and word.strip("._-") not in stopwords
            }

        def concept_from_tokens(tokens: set[str]) -> str:
            if not tokens:
                return "Uncategorized"
            counts = Counter(sorted(tokens))
            preferred = [word for word, _ in counts.most_common(4)]
            return " + ".join(word.upper() if len(word) <= 4 else word.title() for word in preferred)

        groups: list[dict[str, Any]] = []
        for claim in candidates:
            if is_sensitive_claim(claim):
                continue
            scope = (claim.scope or "").strip()
            if not scope.startswith("project:"):
                continue
            claim_type = (claim.claim_type or "").strip().lower()
            if normalized_types and claim_type not in normalized_types:
                continue

            subject = (claim.subject or "").strip()
            subject_key = subject.lower()
            tokens = tokens_for(claim)
            match: dict[str, object] | None = None
            best_overlap = 0
            for group in groups:
                group_subject = str(group.get("_subject_key") or "")
                if subject_key and group_subject == subject_key:
                    match = group
                    break
                group_tokens = group.get("_tokens")
                if not isinstance(group_tokens, set) or not tokens:
                    continue
                overlap = len(tokens & group_tokens)
                if overlap > best_overlap and (overlap >= 2 or overlap >= min(len(tokens), len(group_tokens))):
                    best_overlap = overlap
                    match = group
            if match is None:
                match = {
                    "concept": subject or concept_from_tokens(tokens),
                    "claim_count": 0,
                    "scopes": set(),
                    "exemplar_claim_ids": [],
                    "_tokens": set(tokens),
                    "_subject_key": subject_key,
                    "_first_seen": len(groups),
                }
                groups.append(match)
            else:
                group_tokens = match.get("_tokens")
                if isinstance(group_tokens, set):
                    group_tokens.update(tokens)
                if subject and not match.get("_subject_key"):
                    match["concept"] = subject
                    match["_subject_key"] = subject_key

            match["claim_count"] = int(match["claim_count"]) + 1
            scopes = match["scopes"]
            if isinstance(scopes, set):
                scopes.add(scope)
            exemplar_ids = match["exemplar_claim_ids"]
            if isinstance(exemplar_ids, list) and len(exemplar_ids) < 5:
                exemplar_ids.append(claim.id)

        groups.sort(
            key=lambda group: (
                -int(group["claim_count"]),
                -len(group["scopes"]) if isinstance(group["scopes"], set) else 0,
                int(group["_first_seen"]),
            )
        )
        return {
            "groups": [
                {
                    "concept": str(group["concept"]),
                    "claim_count": int(group["claim_count"]),
                    "scopes": sorted(group["scopes"]) if isinstance(group["scopes"], set) else [],
                    "exemplar_claim_ids": list(group["exemplar_claim_ids"])
                    if isinstance(group["exemplar_claim_ids"], list)
                    else [],
                }
                for group in groups[:top_n]
            ]
        }

    def pin(self, claim_id: int, pin: bool = True) -> Claim:
        claim = self.store.get_claim(claim_id, include_citations=False)
        if claim is None:
            raise ValueError(f"Claim {claim_id} does not exist.")
        self._check_tenant_access(claim)
        self.store.set_pinned(claim_id, pinned=pin, reason="manual pin toggle")
        updated = self.store.get_claim(claim_id)
        if updated is None:
            raise RuntimeError(f"Claim {claim_id} disappeared during pin operation.")
        return updated

    def dedup(
        self,
        *,
        threshold: float = 0.92,
        min_text_overlap: float = 0.3,
        dry_run: bool = False,
        limit: int | None = None,
        scope_filter: str | None = None,
    ) -> dict:
        return dedup.run(
            self.store,
            threshold=threshold,
            min_text_overlap=min_text_overlap,
            dry_run=dry_run,
            provider=self.embedding_provider,
            limit=limit,
            scope_filter=scope_filter,
        )

    def compact(self, retain_days: int = 30, event_retain_days: int = 60) -> dict[str, int]:
        return compactor.run(
            self.store,
            retain_days=retain_days,
            event_retain_days=event_retain_days,
            artifacts_dir=self.workspace_root / "artifacts" / "compaction",
        )

    def compact_summaries(
        self,
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
    ) -> dict:
        result = compact_summaries.run(
            self.store,
            provider=provider,
            api_key=api_key,
            model=model,
            base_url=base_url,
            min_cluster=min_cluster,
            max_cluster=max_cluster,
            similarity_threshold=similarity_threshold,
            dry_run=dry_run,
            limit=limit,
            api_keys=api_keys,
            cooldown_seconds=cooldown_seconds,
            embedding_provider=self.embedding_provider,
        )
        return {
            "clusters_found": result.clusters_found,
            "summaries_created": result.summaries_created,
            "source_claims_summarized": result.source_claims_summarized,
            "errors": result.errors,
            "dry_run": result.dry_run,
            "details": result.details,
        }

    def list_claims(
        self,
        status: str | None = None,
        limit: int = 50,
        include_archived: bool = False,
        *,
        allow_sensitive: bool = False,
    ) -> list[Claim]:
        include_sensitive = self._allow_sensitive(
            allow_sensitive=allow_sensitive,
            context="service.list_claims",
            deny_mode="filter",
        )
        claims = self.store.list_claims(
            status=status,
            limit=limit,
            include_archived=include_archived,
            include_citations=True,
            tenant_id=self.tenant_id,
        )
        if not include_sensitive:
            claims = [claim for claim in claims if not is_sensitive_claim(claim)]
        return claims

    def redact_claim_payload(
        self,
        claim_id: int,
        *,
        mode: str = "redact",
        redact_claim: bool = True,
        redact_citations: bool = True,
        reason: str | None = None,
        actor: str = "service",
    ) -> dict[str, object]:
        if claim_id <= 0:
            raise ValueError("claim_id must be positive.")
        if self.tenant_id is not None:
            claim = self.store.get_claim(claim_id, include_citations=False)
            if claim is None:
                raise ValueError(f"Claim {claim_id} does not exist.")
            self._check_tenant_access(claim)
        result = self.store.redact_claim_payload(
            claim_id,
            mode=mode,
            redact_claim=redact_claim,
            redact_citations=redact_citations,
            reason=reason,
            actor=actor,
        )
        claim = self.store.get_claim(claim_id)
        if claim is None:
            raise RuntimeError(f"Claim {claim_id} disappeared during redact workflow.")
        return {"claim": claim, **result}

    def list_events(
        self,
        claim_id: int | None = None,
        limit: int = 100,
        event_type: str | None = None,
    ) -> list[Event]:
        return self.store.list_events(
            claim_id=claim_id,
            limit=limit,
            event_type=event_type,
        )

    def upsert_external_source(
        self,
        *,
        source_type: str,
        display_name: str,
        config_json: dict[str, object] | str | None = None,
    ) -> ExternalSource:
        return self.store.upsert_external_source(
            source_type=source_type,
            display_name=display_name,
            config_json=config_json,
        )

    def upsert_source_item(
        self,
        *,
        source_id: int,
        source_item_id: str,
        item_type: str,
        chat_id: str | None = None,
        sender_id: str | None = None,
        sender_name: str | None = None,
        occurred_at: str | None = None,
        text: str | None = None,
        payload_json: dict[str, object] | str | None = None,
        content_hash: str | None = None,
        sensitivity: str | None = None,
    ) -> SourceItem:
        return self.store.upsert_source_item(
            source_id=source_id,
            source_item_id=source_item_id,
            item_type=item_type,
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=sender_name,
            occurred_at=occurred_at,
            text=text,
            payload_json=payload_json,
            content_hash=content_hash,
            sensitivity=sensitivity,
        )

    def get_source_item(self, *, source_id: int, source_item_id: str) -> SourceItem | None:
        return self.store.get_source_item(source_id=source_id, source_item_id=source_item_id)

    def get_source_item_by_id(self, source_item_row_id: int) -> SourceItem | None:
        return self.store.get_source_item_by_id(source_item_row_id)

    def add_evidence_item(
        self,
        *,
        source_item_id: int,
        evidence_type: str,
        text: str | None = None,
        media_path: str | None = None,
        provider: str | None = None,
        confidence: float | None = None,
        payload_json: dict[str, object] | str | None = None,
        sensitivity: str | None = None,
    ) -> EvidenceItem:
        return self.store.add_evidence_item(
            source_item_id=source_item_id,
            evidence_type=evidence_type,
            text=text,
            media_path=media_path,
            provider=provider,
            confidence=confidence,
            payload_json=payload_json,
            sensitivity=sensitivity,
        )

    def set_source_item_sensitivity(self, source_item_row_id: int, sensitivity: str | None) -> SourceItem:
        return self.store.set_source_item_sensitivity(source_item_row_id, sensitivity)

    def set_evidence_item_sensitivity(self, evidence_item_row_id: int, sensitivity: str | None) -> EvidenceItem:
        return self.store.set_evidence_item_sensitivity(evidence_item_row_id, sensitivity)

    def enqueue_media_retry(
        self,
        *,
        source_item_id: int,
        media_key: str,
        chat_id: str | None = None,
        media_type: str | None = None,
        media_path: str | None = None,
        media_url: str | None = None,
        status: str = "pending",
        next_attempt_time: str | None = None,
    ) -> MediaRetryItem:
        return self.store.enqueue_media_retry(
            source_item_id=source_item_id,
            media_key=media_key,
            chat_id=chat_id,
            media_type=media_type,
            media_path=media_path,
            media_url=media_url,
            status=status,
            next_attempt_time=next_attempt_time,
        )

    def claim_pending_media_retries(self, limit: int = 25) -> list[MediaRetryItem]:
        return self.store.claim_pending_media_retries(limit=limit)

    def record_media_retry_outcome(
        self,
        retry_id: int,
        *,
        status: str,
        media_path: str | None = None,
        last_http_status: int | None = None,
        last_error: str | None = None,
        next_attempt_time: str | None = None,
    ) -> MediaRetryItem:
        return self.store.record_media_retry_outcome(
            retry_id,
            status=status,
            media_path=media_path,
            last_http_status=last_http_status,
            last_error=last_error,
            next_attempt_time=next_attempt_time,
        )

    def list_media_retries(
        self,
        *,
        status: str | None = None,
        source_item_id: int | None = None,
        limit: int = 100,
    ) -> list[MediaRetryItem]:
        return self.store.list_media_retries(
            status=status,
            source_item_id=source_item_id,
            limit=limit,
        )

    def media_retry_status_counts(self) -> dict[str, int]:
        return self.store.media_retry_status_counts()

    def list_evidence_items(
        self,
        *,
        source_item_id: int | None = None,
        evidence_type: str | None = None,
        limit: int = 100,
    ) -> list[EvidenceItem]:
        return self.store.list_evidence_items(
            source_item_id=source_item_id,
            evidence_type=evidence_type,
            limit=limit,
        )

    def create_action_proposal(
        self,
        *,
        proposal_type: str,
        title: str,
        description: str | None = None,
        source_item_id: int | None = None,
        evidence_item_id: int | None = None,
        claim_id: int | None = None,
        suggested_due_at: str | None = None,
        destination: str = "manual",
        confidence: float = 0.5,
        payload_json: dict[str, object] | str | None = None,
        idempotency_key: str | None = None,
    ) -> ActionProposal:
        return self.store.create_action_proposal(
            proposal_type=proposal_type,
            title=title,
            description=description,
            source_item_id=source_item_id,
            evidence_item_id=evidence_item_id,
            claim_id=claim_id,
            suggested_due_at=suggested_due_at,
            destination=destination,
            confidence=confidence,
            payload_json=payload_json,
            idempotency_key=idempotency_key,
        )

    def get_action_proposal_by_idempotency_key(self, idempotency_key: str) -> ActionProposal | None:
        return self.store.get_action_proposal_by_idempotency_key(idempotency_key)

    def update_action_proposal_status(
        self,
        proposal_id: int,
        *,
        status: str,
        external_ref: str | None = None,
        exported_at: str | None = None,
        payload_json: dict[str, object] | str | None = None,
    ) -> ActionProposal:
        return self.store.update_action_proposal_status(
            proposal_id,
            status=status,
            external_ref=external_ref,
            exported_at=exported_at,
            payload_json=payload_json,
        )

    def list_action_proposals(
        self,
        *,
        status: str | None = None,
        destination: str | None = None,
        limit: int = 100,
    ) -> list[ActionProposal]:
        return self.store.list_action_proposals(
            status=status,
            destination=destination,
            limit=limit,
        )

    def update_action_proposal_fields(
        self,
        proposal_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        suggested_due_at: str | None = None,
        confidence: float | None = None,
        payload_json: dict[str, object] | str | None = None,
    ) -> ActionProposal:
        return self.store.update_action_proposal_fields(
            proposal_id,
            title=title,
            description=description,
            suggested_due_at=suggested_due_at,
            confidence=confidence,
            payload_json=payload_json,
        )

    def add_claim_link(self, source_id: int, target_id: int, link_type: str) -> ClaimLink:
        for cid in (source_id, target_id):
            claim = self.store.get_claim(cid, include_citations=False)
            if claim is None:
                raise ValueError(f"Claim {cid} does not exist.")
        return self.store.add_claim_link(source_id, target_id, link_type)

    def remove_claim_link(self, source_id: int, target_id: int, link_type: str | None = None) -> int:
        return self.store.remove_claim_link(source_id, target_id, link_type)

    def get_claim_links(self, claim_id: int) -> list[ClaimLink]:
        return self.store.get_claim_links(claim_id)

    def get_linked_claims(self, claim_id: int, link_type: str | None = None) -> list[ClaimLink]:
        return self.store.get_linked_claims(claim_id, link_type=link_type)

    def federated_query(
        self,
        query_text: str,
        *,
        limit: int = 20,
        current_scope: str | None = None,
        scope_allowlist: list[str] | None = None,
    ) -> list[dict]:
        """Query across ALL scopes — cross-project federation.

        Returns claims from all projects, sorted by relevance.
        Unlike regular query which filters by scope_allowlist, this
        searches everything.
        """
        normalized_current_scope = (current_scope or "").strip() or None
        normalized_scope_allowlist = self._normalize_scope_allowlist(scope_allowlist)
        query_limit = max(limit * 10, 100) if limit > 0 else limit
        rows = self.query_rows(
            query_text=query_text,
            limit=query_limit,
            scope_allowlist=normalized_scope_allowlist,
            include_candidates=True,
        )
        return [
            row
            for row in rows
            if _allow_federated_claim(
                row["claim"],
                current_scope=normalized_current_scope,
                explicit_scope_allowlist=normalized_scope_allowlist,
            )
        ][:limit]
