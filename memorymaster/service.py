from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from memorymaster.embeddings import EmbeddingProvider
from memorymaster.jobs import compactor, decay, deterministic, extractor, validator
from memorymaster.models import CitationInput, Claim, Event
from memorymaster.policy import select_revalidation_candidates
from memorymaster.retrieval import VectorSearchHook, rank_claim_rows
from memorymaster.security import is_sensitive_claim, resolve_allow_sensitive_access, sanitize_claim_input
from memorymaster.store_factory import create_store


class MemoryService:
    def __init__(
        self,
        db_target: str | Path,
        workspace_root: str | Path | None = None,
        *,
        policy_config: Mapping[str, object] | None = None,
    ) -> None:
        self.store = create_store(db_target)
        self.workspace_root = Path(workspace_root) if workspace_root else Path.cwd()
        self.embedding_provider = EmbeddingProvider()
        self.policy_config = policy_config

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
        return {
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
        )
        if not include_sensitive:
            candidates = [claim for claim in candidates if not is_sensitive_claim(claim)]
        if vector_hook is None and hasattr(self.store, "vector_scores"):
            vector_hook = lambda text, claims: self.store.vector_scores(text, claims, self.embedding_provider)
        ranked_rows = rank_claim_rows(
            query_text,
            candidates,
            mode=retrieval_mode,
            limit=limit,
            vector_hook=vector_hook,
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

    def pin(self, claim_id: int, pin: bool = True) -> Claim:
        claim = self.store.get_claim(claim_id, include_citations=False)
        if claim is None:
            raise ValueError(f"Claim {claim_id} does not exist.")
        self.store.set_pinned(claim_id, pinned=pin, reason="manual pin toggle")
        updated = self.store.get_claim(claim_id)
        if updated is None:
            raise RuntimeError(f"Claim {claim_id} disappeared during pin operation.")
        return updated

    def compact(self, retain_days: int = 30, event_retain_days: int = 60) -> dict[str, int]:
        return compactor.run(
            self.store,
            retain_days=retain_days,
            event_retain_days=event_retain_days,
            artifacts_dir=self.workspace_root / "artifacts" / "compaction",
        )

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
