from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import logging
import os

from memorymaster.embeddings import create_best_provider
from memorymaster.jobs import compact_summaries, compactor, decay, dedup, deterministic, extractor, validator
from memorymaster.models import CitationInput, Claim, ClaimLink, Event
from memorymaster.policy import select_revalidation_candidates
from memorymaster.context_optimizer import ContextResult, pack_context
from memorymaster.retrieval import VectorSearchHook, rank_claim_rows
from memorymaster.security import is_sensitive_claim, resolve_allow_sensitive_access, sanitize_claim_input
from memorymaster.store_factory import create_store

logger = logging.getLogger(__name__)


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
        self.embedding_provider = create_best_provider()
        self.policy_config = policy_config
        self.tenant_id = (tenant_id or "").strip() or None
        self.qdrant = self._init_qdrant()

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
    ) -> Claim:
        if not text.strip():
            raise ValueError("Claim text cannot be empty.")
        if not citations:
            raise ValueError("At least one citation is required.")
        normalized_idempotency_key = (idempotency_key or "").strip() or None
        if normalized_idempotency_key is not None and hasattr(self.store, "get_claim_by_idempotency_key"):
            existing_claim = self.store.get_claim_by_idempotency_key(normalized_idempotency_key)
            if existing_claim is not None:
                return existing_claim
        sanitized = sanitize_claim_input(
            text=text.strip(),
            object_value=object_value,
            citations=citations,
        )
        if not sanitized.citations:
            raise ValueError("At least one citation is required.")
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
        )
        if sanitized.is_sensitive:
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
        policy_selection = select_revalidation_candidates(
            self.store,
            mode=policy_mode,
            limit=policy_limit,
        )
        extract_res = extractor.run(self.store)
        deterministic_res = deterministic.run(
            self.store,
            workspace_root=self.workspace_root,
            revalidation_claims=policy_selection.selected,
            policy_mode=policy_mode,
        )
        validate_res = validator.run(
            self.store,
            min_citations=min_citations,
            min_score=min_score,
            revalidation_claims=policy_selection.selected,
            policy_mode=policy_mode,
        )
        decay_res = decay.run(self.store)
        compact_res = (
            compactor.run(
                self.store,
                artifacts_dir=self.workspace_root / "artifacts" / "compaction",
            )
            if run_compactor
            else {"archived_claims": 0, "deleted_events": 0}
        )
        result = {
            "policy": {
                "mode": policy_selection.mode,
                "considered": policy_selection.considered,
                "due": policy_selection.due,
                "selected": len(policy_selection.selected),
            },
            "extractor": extract_res,
            "deterministic": deterministic_res,
            "validator": validate_res,
            "decay": decay_res,
            "compactor": compact_res,
        }
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
        allow_sensitive: bool = False,
        scope_allowlist: list[str] | None = None,
    ) -> list[Claim]:
        rows = self.query_rows(
            query_text=query_text,
            limit=limit,
            include_stale=include_stale,
            include_conflicted=include_conflicted,
            include_candidates=include_candidates,
            retrieval_mode=retrieval_mode,
            vector_hook=vector_hook,
            allow_sensitive=allow_sensitive,
            scope_allowlist=scope_allowlist,
        )
        return [row["claim"] for row in rows]

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
        allow_sensitive: bool = False,
        scope_allowlist: list[str] | None = None,
    ) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        include_sensitive = self._allow_sensitive(
            allow_sensitive=allow_sensitive,
            context="service.query_rows",
            deny_mode="filter",
        )

        statuses = ["confirmed"]
        if include_stale:
            statuses.append("stale")
        if include_conflicted:
            statuses.append("conflicted")
        if include_candidates:
            statuses.append("candidate")
        normalized_scopes = self._normalize_scope_allowlist(scope_allowlist)

        if retrieval_mode == "legacy":
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
            ranked_rows = rank_claim_rows(
                query_text,
                legacy,
                mode="legacy",
                limit=limit,
                vector_hook=None,
            )
            return [
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

        candidate_limit = max(limit * 6, 60)
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
        ranked_rows = rank_claim_rows(
            query_text,
            candidates,
            mode=retrieval_mode,
            limit=limit,
            vector_hook=vector_hook,
            semantic_vectors=semantic,
        )
        return [
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
        allow_sensitive: bool = False,
        scope_allowlist: list[str] | None = None,
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
        """
        rows = self.query_rows(
            query_text=query,
            limit=limit,
            include_stale=include_stale,
            include_conflicted=include_conflicted,
            include_candidates=include_candidates,
            retrieval_mode=retrieval_mode,
            allow_sensitive=allow_sensitive,
            scope_allowlist=scope_allowlist,
        )
        return pack_context(
            rows,
            token_budget=token_budget,
            output_format=output_format,
        )

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
    ) -> dict:
        return dedup.run(
            self.store,
            threshold=threshold,
            min_text_overlap=min_text_overlap,
            dry_run=dry_run,
            provider=self.embedding_provider,
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
