from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import logging
import os

from memorymaster.core import lifecycle, observability
from memorymaster.core import llm_budget
from memorymaster.govern import candidate_dedupe
from memorymaster.recall import query_cache
from memorymaster.recall.embeddings import EmbeddingProvider, create_best_provider
from memorymaster.govern.jobs import compact_summaries, compactor, decay, dedup, deterministic, extractor, integrity, qdrant_reconcile, spool_drain, validator
from memorymaster.core.models import ActionProposal, CitationInput, Claim, ClaimLink, Event, EvidenceItem, ExternalSource, MediaRetryItem, SourceItem, validate_temporal_fields
from memorymaster.core.policy import select_revalidation_candidates
from memorymaster.recall.context_optimizer import ContextResult, pack_context
from memorymaster.core.config import get_config
from memorymaster.recall.retrieval import VectorSearchHook, _tier_bonus, rank_claim_rows
from memorymaster.core.security import is_sensitive_claim, resolve_allow_sensitive_access, sanitize_claim_input
from memorymaster.core.intake_policy import IntakeRejected, evaluate_intake
from memorymaster.stores.store_factory import create_store
import contextlib

logger = logging.getLogger(__name__)


def _wiki_autopromote_adapter(claim_id: int, db_path: str | None = None) -> None:
    """Lazy adapter for lifecycle's wiki autopromote hook.

    P2 phase0 cycle cut: lifecycle (core) must never import wiki_engine
    (knowledge), so service wiring registers this callback instead. The import
    stays inside the function so wiki_engine is only loaded when the
    autopromote threshold actually fires (same laziness as the old
    lifecycle-internal import).
    """
    from memorymaster.knowledge.wiki_engine import absorb_single_claim

    absorb_single_claim(claim_id, db_path=db_path)


if lifecycle.on_claim_confirmed is None:
    lifecycle.on_claim_confirmed = _wiki_autopromote_adapter

RetrievalWeights = tuple[float, float, float, float]

# Rule-mining steward phase (P3). DEFAULT OFF: run_cycle only mines verbatim
# corrections into rule candidates when MEMORYMASTER_STEWARD_RULE_MINING is
# explicitly enabled. When unset/off, run_cycle makes ZERO rule-mining LLM
# calls and behaves exactly as before. When on, the per-cycle window cap below
# bounds how many candidate windows (and thus LLM calls) the phase examines.
_RULE_MINING_FLAG = "MEMORYMASTER_STEWARD_RULE_MINING"
_RULE_MINING_LIMIT_ENV = "MEMORYMASTER_STEWARD_RULE_MINING_LIMIT"
_RULE_MINING_DEFAULT_LIMIT = 25


def _rule_mining_enabled() -> bool:
    """True only when the gate flag is explicitly truthy. Default is OFF."""
    raw = os.environ.get(_RULE_MINING_FLAG, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _rule_mining_limit() -> int:
    """Conservative per-cycle cap on candidate windows examined (bounds LLM
    calls). Falls back to the default when unset or unparseable; never < 1."""
    raw = os.environ.get(_RULE_MINING_LIMIT_ENV, "").strip()
    if not raw:
        return _RULE_MINING_DEFAULT_LIMIT
    try:
        return max(1, int(raw))
    except ValueError:
        return _RULE_MINING_DEFAULT_LIMIT


# Hard ceiling for BFS path traversal — clamps caller-supplied max_hops so a
# pathological request cannot fan out across the whole graph. Five hops is well
# beyond any provenance/conflict/impact chain we expect in practice.
MAX_CLAIM_PATH_HOPS = 5


def _claim_to_path_dict(claim: Claim) -> dict[str, Any]:
    """Serialize a Claim to a plain dict for path-query results (no citations)."""
    from dataclasses import asdict

    data = asdict(claim)
    data.pop("citations", None)
    return data


def _weakest_link_confidence(path: list[int], conf_by_id: dict[int, float]) -> float:
    """Weakest-link roll-up: minimum claim confidence across the whole path.

    A path is only as trustworthy as its least-confident claim. Missing
    confidences default to 0.0 so an unknown hop cannot inflate the score.
    """
    if not path:
        return 0.0
    return min(conf_by_id.get(cid, 0.0) for cid in path)


def _edge_chain_for_path(
    path: list[int],
    raw: list[dict[str, Any]],
    entry: dict[str, Any],
) -> list[str]:
    """Reconstruct the link types traversed along ``path`` (start → node).

    Each BFS entry records the single ``link_type`` of the edge used to REACH
    that node. We look up that edge per intermediate node so a 2-hop path
    surfaces both hops' types (e.g. ``["derived_from", "supports"]``).
    """
    link_for_node = {e["path"][-1]: e.get("link_type") for e in raw if e.get("path")}
    link_for_node[path[-1]] = entry.get("link_type")
    # path[0] is the start node (no inbound edge); chain is one type per hop.
    return [lt for cid in path[1:] if (lt := link_for_node.get(cid)) is not None]


def _path_direction_to_traverse(direction: str) -> str:
    """Map the public path-query direction to traverse_relationships direction.

    Public API uses ``in``/``out``/``both`` (provenance/impact/all); the storage
    BFS uses ``incoming``/``outgoing``/``both``. Unknown values fall back to
    ``both`` rather than raising — a path query should degrade, not crash.
    """
    return {"in": "incoming", "out": "outgoing", "both": "both"}.get(
        (direction or "both").strip().lower(), "both"
    )

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


def _filter_agent_visibility(claims: list[Claim], requesting_agent: str | None) -> list[Claim]:
    """Drop other agents' PRIVATE claims for a per-agent visibility query.

    A claim is visible to ``requesting_agent`` when it is public OR was authored
    by that same agent. With no requesting agent the list is returned unchanged.
    Must be applied on EVERY retrieval path (legacy, hybrid, cache rehydrate) or
    a path-dependent cross-agent leak appears.
    """
    if not requesting_agent:
        return claims
    return [
        c
        for c in claims
        if getattr(c, "visibility", "public") == "public"
        or getattr(c, "source_agent", None) == requesting_agent
    ]


def _is_cross_scope_sensitive(claim: Claim, current_scope: str | None) -> bool:
    visibility = (getattr(claim, "visibility", "public") or "public").strip().lower()
    if visibility != "sensitive":
        return False
    claim_scope = (claim.scope or "").strip()
    return not current_scope or claim_scope != current_scope


def _path_claim_in_scope(
    claim: Claim,
    scope_allowlist: list[str] | None,
    allow_sensitive: bool,
) -> bool:
    """Gate a claim-path traversal hit by scope + sensitivity.

    Drops claims outside ``scope_allowlist`` (when one is given) and, unless
    ``allow_sensitive``, drops any sensitive-visibility claim — so traversal
    from a known claim_id can't leak cross-scope or sensitive text.
    """
    claim_scope = (claim.scope or "").strip()
    if scope_allowlist is not None and claim_scope not in scope_allowlist:
        return False
    if not allow_sensitive:
        visibility = (getattr(claim, "visibility", "public") or "public").strip().lower()
        if visibility == "sensitive":
            return False
    return True


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
        from memorymaster.recall.llm_rerank import rerank_temporarily_disabled

        return not rerank_temporarily_disabled()
    except Exception:
        return True


def _recall_weights_snapshot(query_type: str | None) -> dict[str, Any]:
    """Snapshot the retrieval weights in force, for recall explainability.

    Reads ``get_config()`` (the same source the ranker uses) so the reported
    weights match what actually scored the claims. Includes the per-query-type
    profile override when one applies.
    """
    cfg = get_config()
    lex, conf, fresh, vec = cfg.retrieval_weights
    no_vec = cfg.retrieval_weights_no_vector
    profile = cfg.retrieval_profile(query_type) if query_type else None
    return {
        "retrieval_weights": {
            "lexical": lex,
            "confidence": conf,
            "freshness": fresh,
            "vector": vec,
        },
        "retrieval_weights_no_vector": {
            "lexical": no_vec[0],
            "confidence": no_vec[1],
            "freshness": no_vec[2],
        },
        "query_type": query_type,
        "profile_override": (
            None
            if profile is None
            else {
                "lexical": profile[0],
                "confidence": profile[1],
                "freshness": profile[2],
                "vector": profile[3],
            }
        ),
        "pinned_bonus": cfg.pinned_bonus,
        "boost_floor_ratio": cfg.boost_floor_ratio,
    }


def _recall_component_rankings(rows: list[dict[str, Any]]) -> dict[str, list[int]]:
    """Per-component claim rankings for the returned rows (best-first ids).

    Rebuilds lightweight ``RankedClaim`` objects from the query_rows dicts so
    the shared ``retrieval.component_rankings`` logic can be reused without
    duplicating the sort. Pure read — does not reorder ``rows``.
    """
    from memorymaster.recall.retrieval import RankedClaim, component_rankings

    ranked = [
        RankedClaim(
            claim=row["claim"],
            score=float(row.get("score", 0.0)),
            lexical_score=float(row.get("lexical_score", 0.0)),
            freshness_score=float(row.get("freshness_score", 0.0)),
            confidence_score=float(row.get("confidence_score", 0.0)),
            vector_score=float(row.get("vector_score", 0.0)),
            breakdown=row.get("breakdown"),
        )
        for row in rows
        if row.get("claim") is not None
    ]
    if not ranked:
        return {}
    return component_rankings(ranked)


def _recall_result_entry(row: dict[str, Any]) -> dict[str, Any]:
    """Project one query_rows dict into a recall-analysis result entry."""
    claim = row["claim"]
    return {
        "claim_id": getattr(claim, "id", None),
        "human_id": getattr(claim, "human_id", None),
        "text": getattr(claim, "text", ""),
        "status": row.get("status", getattr(claim, "status", None)),
        "tier": getattr(claim, "tier", "working"),
        "pinned": bool(getattr(claim, "pinned", False)),
        "score": float(row.get("score", 0.0)),
        "lexical_score": float(row.get("lexical_score", 0.0)),
        "confidence_score": float(row.get("confidence_score", 0.0)),
        "freshness_score": float(row.get("freshness_score", 0.0)),
        "vector_score": float(row.get("vector_score", 0.0)),
        "breakdown": row.get("breakdown"),
    }


class MemoryService:
    def __init__(
        self,
        db_target: str | Path,
        workspace_root: str | Path | None = None,
        *,
        policy_config: Mapping[str, object] | None = None,
        tenant_id: str | None = None,
        read_only: bool = False,
    ) -> None:
        # read_only (P1 WAL-discipline, spec §2.2): SQLite store opens
        # mode=ro + query_only connections; _record_accesses spools its
        # access/feedback signal instead of writing. Used by the per-prompt
        # recall hook under MEMORYMASTER_WAL_DISCIPLINE=1.
        self.store = create_store(db_target, read_only=read_only)
        self.workspace_root = Path(workspace_root) if workspace_root else Path.cwd()
        self._embedding_provider: EmbeddingProvider | None = None
        self.policy_config = policy_config
        self.tenant_id = (tenant_id or "").strip() or None
        # Rollup telemetry (additive, default-safe): when set by a surface,
        # recall events are attributed to this agent / session for the usage
        # rollup. Default None keeps recall behaviour byte-identical.
        self.source_agent: str | None = None
        self.session_id: int | None = None
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
            from memorymaster.recall.qdrant_backend import QdrantBackend
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
        holder: str | None = None,
        require_source_agent: bool = False,
        intake_batch_id: str | None = None,
        intake_batch_max: int | None = None,
    ) -> Claim:
        if not text.strip():
            raise ValueError("Claim text cannot be empty.")
        # Bitemporal write-time guard: reject malformed ISO-8601 or an inverted
        # validity interval at the boundary, before any dedup/sanitize work, so
        # a durable-but-invisible row (valid_until < valid_from) never reaches
        # the store. Backend-agnostic — both SQLite and Postgres ingest here.
        validate_temporal_fields(event_time, valid_from, valid_until)
        # Single-bound hole (fresh-eyes audit 2026-07-01): valid_until passed
        # ALONE sails past the pairwise guard, then the store auto-populates
        # valid_from=now — so a past valid_until yields a BORN-INVERTED row
        # (valid_until < valid_from), the exact state the guard blocks.
        # Backdate valid_from to valid_until (zero-width interval, valid) so
        # "this stopped being true at X" ingests successfully and can never
        # invert. Future valid_until keeps the auto-populate (now < until).
        if valid_until and not valid_from:
            from datetime import datetime, timezone

            from memorymaster.core.models import _parse_iso_strict

            _vu = _parse_iso_strict("valid_until", valid_until)
            if _vu is not None and _vu <= datetime.now(timezone.utc):
                valid_from = valid_until
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
            subject=subject,
            predicate=predicate,
        )
        if not sanitized.citations:
            raise ValueError("At least one citation is required.")
        # Use the sanitized subject/predicate everywhere downstream so a secret
        # placed in those fields is redacted at rest, not just at display time.
        subject = sanitized.subject
        predicate = sanitized.predicate
        # Intake policy (P3) — runs AFTER the sacred sensitivity filter above and
        # BEFORE create_claim. Additive admission control: may reject more or
        # default-tag attribution, never weakens the filter or flips a prior
        # reject into an accept. Reject raises IntakeRejected (a ValueError) so
        # existing `except ValueError` handlers surface VALIDATION_ERROR.
        try:
            decision = evaluate_intake(
                text=sanitized.text,
                claim_type=claim_type,
                subject=subject,
                scope=scope,
                source_agent=source_agent,
                require_source_agent=require_source_agent,
                intake_batch_id=intake_batch_id,
                intake_batch_max=intake_batch_max,
            )
        except IntakeRejected as rejected:
            observability.bump_claim_policy_rejected(rejected.rule, rejected.reason)
            try:
                self.store.record_event(
                    claim_id=None,
                    event_type="policy_decision",
                    details=f"intake_rejected:{rejected.rule}",
                    payload={
                        "rule": rejected.rule,
                        "reason": rejected.reason,
                        "scope": scope,
                        "claim_type": claim_type,
                        "source_agent": source_agent or "",
                    },
                )
            except Exception:
                pass  # event recording must never mask the rejection
            raise
        new_source_agent = decision.mutated_fields.get("source_agent")
        if isinstance(new_source_agent, str):
            source_agent = new_source_agent
        # Resolve subject → canonical entity (GBrain-inspired entity registry)
        # and mine text for pattern-based entities (#127 Wave 3).
        entity_id = 0
        if subject or sanitized.text:
            try:
                from memorymaster.knowledge.entity_registry import (
                    add_alias,
                    resolve_or_create,
                )
                from memorymaster.knowledge.entity_extractor import extract_patterns

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
            holder=holder,
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
            from memorymaster.core.webhook import fire_webhook

            fire_webhook(
                "claim_ingested",
                {"claim_id": claim.id, "text": claim.text[:200], "status": claim.status},
            )
        except Exception:
            pass
        observability.bump_claim_ingested(source_agent)
        return claim

    def _rule_mining_phase(self) -> dict[str, object]:
        """Mine verbatim corrections into rule candidates (P3 steward phase).

        DEFAULT OFF via ``MEMORYMASTER_STEWARD_RULE_MINING``: when the flag is
        unset/off this returns ``{"enabled": False}`` and makes NO LLM calls —
        run_cycle behaves exactly as before. When on, it delegates to
        :func:`rule_miner.mine_rules`, which already redacts/drops sensitive
        rules and ingests through ``service.ingest`` (intake policy +
        sensitivity filter both apply). The per-cycle window cap bounds LLM
        calls; the call runs inside run_cycle's open ``cycle_scope`` so the
        global budget caps still abort it cleanly.

        Resilience: any failure (LLM error, missing verbatim table, Postgres
        store, etc.) is caught and surfaced as ``{"error": ...}`` so the rest
        of the cycle (decay, integrity, ...) still completes.
        """
        if not _rule_mining_enabled():
            return {"enabled": False}

        db_path = str(getattr(self.store, "db_path", "") or "")
        if not db_path or "://" in db_path:
            # Verbatim rule mining is SQLite-only; skip cleanly for Postgres
            # stores or stores without a usable SQLite path.
            return {"enabled": True, "skipped": "no_sqlite_db_path"}

        from memorymaster.knowledge import rule_miner

        result = rule_miner.mine_rules(
            db_path,
            self,
            limit=_rule_mining_limit(),
        )
        result["enabled"] = True
        return result

    def run_cycle(
        self,
        *,
        run_compactor: bool = False,
        min_citations: int = 1,
        min_score: float = 0.58,
        policy_mode: str = "legacy",
        policy_limit: int = 200,
        batch_limit: int = 200,
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
                extract_res = extractor.run(self.store, limit=batch_limit)
                result["policy"] = {
                    "mode": policy_selection.mode,
                    "considered": policy_selection.considered,
                    "due": policy_selection.due,
                    "selected": len(policy_selection.selected),
                }
                result["extractor"] = extract_res
                # Match validator's scan size (200) so every candidate the
                # validator would touch gets a chance to dedupe first.
                dedupe_res = candidate_dedupe.run(self.store, limit=batch_limit)
                result["dedupe"] = dedupe_res
                deterministic_res = deterministic.run(
                    self.store,
                    workspace_root=self.workspace_root,
                    limit=batch_limit,
                    revalidation_claims=policy_selection.selected,
                    policy_mode=policy_mode,
                )
                result["deterministic"] = deterministic_res
                validate_res = validator.run(
                    self.store,
                    limit=batch_limit,
                    min_citations=min_citations,
                    min_score=min_score,
                    revalidation_claims=policy_selection.selected,
                    policy_mode=policy_mode,
                )
                result["validator"] = validate_res
                decay_res = decay.run(self.store, limit=batch_limit)
                result["decay"] = decay_res
                # Hebbian/Ebbinghaus edge decay (MemPalace forgetting curve) —
                # RECALL-ALTERING, default OFF behind MEMORYMASTER_HEBBIAN_DECAY.
                # Failure-isolated: an entity-graph error must never crash the
                # cycle. When the flag is unset this is a cheap no-op that
                # mutates nothing (result records enabled=False).
                try:
                    result["entity_edge_decay"] = decay.decay_entity_edges(self.store)
                except Exception as exc:
                    logger.warning("entity edge decay phase failed: %s", exc)
                    result["entity_edge_decay"] = {"error": str(exc)}
                compact_res = (
                    compactor.run(
                        self.store,
                        artifacts_dir=self.workspace_root / "artifacts" / "compaction",
                    )
                    if run_compactor
                    else {"archived_claims": 0, "deleted_events": 0}
                )
                result["compactor"] = compact_res
                # Rule-mining steward phase (P3) — DEFAULT OFF. Runs INSIDE the
                # cycle_scope so the global LLM budget caps still abort it
                # cleanly. Failure-isolated: a mine_rules error never crashes
                # the cycle; the remaining phases (decay already ran above;
                # integrity/qdrant/spool below) still complete.
                try:
                    result["rule_mining"] = self._rule_mining_phase()
                except Exception as exc:
                    logger.warning("rule mining phase failed: %s", exc)
                    result["rule_mining"] = {"enabled": True, "error": str(exc)}
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
        # Integrity steward phase (P1 spec §2.5) — checkpoint every cycle,
        # quick_check/fk_check daily, VACUUM INTO weekly. Additive and
        # default-on; never allowed to break the cycle itself.
        try:
            result["integrity"] = integrity.run(self.store)
        except Exception as exc:
            logger.warning("integrity phase failed: %s", exc)
            result["integrity"] = {"error": str(exc)}
        # Qdrant reconciliation (P1 spec §2.7) — daily drift metric; full
        # sync_all + orphan-point delete only when |drift| exceeds
        # MEMORYMASTER_QDRANT_DRIFT_MAX. Clean skip when QDRANT_URL is unset
        # (self.qdrant is None); never allowed to break the cycle itself.
        try:
            result["qdrant_reconcile"] = qdrant_reconcile.run(self.store, self.qdrant)
        except Exception as exc:
            logger.warning("qdrant reconcile phase failed: %s", exc)
            result["qdrant_reconcile"] = {"error": str(exc)}
        # Spool drain (P1 spec §2.4) — replay spooled access/feedback/ingest/
        # verbatim/dream envelopes through the normal service paths (the
        # sensitivity filter applies via svc.ingest). Cheap no-op when the
        # spool is empty; never allowed to break the cycle itself.
        try:
            result["spool_drain"] = spool_drain.run(self)
        except Exception as exc:
            logger.warning("spool drain phase failed: %s", exc)
            result["spool_drain"] = {"error": str(exc)}
        # Tier recomputation — keep recall-weight tiers current every cycle.
        # Runs AFTER spool_drain so freshly-replayed access_count signals are
        # reflected. Fast SQL, no LLM cost. Without this in the cycle, tiers
        # drift: heavy-use claims stay 'working' and never get promoted to
        # 'core', and the 'peripheral' demotion never fires (observed live —
        # 21k claims eligible-for-core but only ~7k actually core, peripheral
        # empty). Failure-isolated like every other phase.
        try:
            result["recompute_tiers"] = self.recompute_tiers()
        except Exception as exc:
            logger.warning("recompute_tiers phase failed: %s", exc)
            result["recompute_tiers"] = {"error": str(exc)}
        # Per-cycle observability snapshot (P1 spec §2.10) — one
        # `integrity_metrics` event aggregating WAL/spool/drift/busy numbers
        # for the dashboard panels and the §7 escalation tripwire. Must run
        # AFTER the three phases above so their results are in `result`.
        try:
            result["integrity_metrics"] = integrity.emit_metrics(self.store, result)
        except Exception as exc:
            logger.warning("integrity metrics emit failed: %s", exc)
            result["integrity_metrics"] = {"error": str(exc)}
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
        from memorymaster.knowledge.rules import parse_rule

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
        legacy = _filter_agent_visibility(legacy, requesting_agent)
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
                "breakdown": row.breakdown,
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
            from memorymaster.core.access_control import require_permission
            require_permission(requesting_agent, "query")

        include_sensitive = self._allow_sensitive(
            allow_sensitive=allow_sensitive,
            context="service.query_rows",
            deny_mode="filter",
        )

        statuses = self._build_query_statuses(include_stale, include_conflicted, include_candidates)
        normalized_scopes = self._normalize_scope_allowlist(scope_allowlist)
        # Intent-aware ranking (plan 1.3): retrieval_profile="auto" derives the
        # weight profile from the query's intent (explicit query_type if given,
        # else rule-based classification). Opt-in only — any other value (incl.
        # None) leaves ranking exactly as before.
        if retrieval_profile == "auto":
            from memorymaster.recall.query_classifier import (
                classify_query,
                profile_for_query_type,
            )
            resolved_type = query_type or classify_query(query_text)
            retrieval_profile = profile_for_query_type(resolved_type)
        profile_weights = _retrieval_profile_weights(retrieval_profile)
        if profile_weights is not None and retrieval_mode == "legacy":
            retrieval_mode = "hybrid"

        if retrieval_mode == "legacy":
            return self._query_legacy_mode(query_text, limit, statuses, normalized_scopes, include_sensitive, requesting_agent)

        use_llm_rerank = _llm_rerank_enabled()

        # Correctness-safe result cache (opt-in, SQLite-only). The key folds in
        # the query params + a config fingerprint; the entry is valid only while
        # the corpus generation (bumped by claim-write triggers) is unchanged.
        cache_path = None
        cache_key = None
        if query_cache.cache_enabled():
            cache_path = query_cache.sqlite_db_path(self.store)
            if cache_path:
                cache_key = query_cache.make_cache_key(query_text, {
                    "limit": limit, "statuses": sorted(statuses), "scopes": normalized_scopes,
                    "mode": retrieval_mode, "profile": retrieval_profile,
                    "sensitive": include_sensitive, "query_type": query_type,
                    "tenant": self.tenant_id or "", "enrich": enrich_with_entities,
                    "llm_rerank": use_llm_rerank,
                    # Per-agent visibility differs per requester; keying on it
                    # prevents serving agentA's PRIVATE claims to agentB.
                    "agent": requesting_agent or "",
                })
                cached = query_cache.read(cache_path, cache_key)
                if cached is not None:
                    rows = self._rehydrate_cached_rows(cached)
                    # Visibility: filter out private claims from other agents
                    # (cache is shared across agents; key omits requesting_agent).
                    if requesting_agent:
                        rows = [
                            r for r in rows
                            if getattr(r["claim"], "visibility", "public") == "public"
                            or getattr(r["claim"], "source_agent", None) == requesting_agent
                        ]
                    self._record_accesses(rows, query_text=query_text)
                    return rows
        # Capture the corpus generation BEFORE reading candidates so the cache
        # entry is tagged with the generation it was actually computed against,
        # not whatever it is after ranking/LLM-rerank (which a concurrent claim
        # write could have bumped). See query_cache.write TOCTOU note.
        cache_generation = query_cache.read_generation(cache_path) if cache_path else 0
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
        # Visibility: filter out private claims from other agents (parity with
        # the legacy path; without this the hybrid path leaks cross-agent data).
        candidates = _filter_agent_visibility(candidates, requesting_agent)
        semantic = False
        if vector_hook is None and hasattr(self.store, "vector_scores"):
            def _vector_hook(text, claims):
                return self.store.vector_scores(text, claims, self.embedding_provider)
            vector_hook = _vector_hook
            semantic = self.embedding_provider.is_semantic
            if semantic:
                # The Gemini->hash downgrade is lazy: is_semantic only reflects a
                # runtime fallback AFTER an embed has run. Probe once with the query
                # (the hook embeds it anyway; embed() handles its own failure and sets
                # degraded) so a degraded provider is not reported as semantic — which
                # would apply retrieval's lenient vector-only filter to non-semantic
                # hash vectors. (audit: embeddings TOCTOU)
                self.embedding_provider.embed(query_text)
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
                "breakdown": row.breakdown,
            }
            for row in ranked_rows
        ]
        if use_llm_rerank:
            from memorymaster.recall.llm_rerank import rerank_with_llm

            results = rerank_with_llm(query_text, results, top_k=limit)
        self._record_accesses(results, query_text=query_text)
        if enrich_with_entities:
            results = self._enrich_with_entity_graph(results, query_text, limit)
        if cache_path and cache_key:
            query_cache.write(cache_path, cache_key, [
                {
                    "id": r["claim"].id,
                    "score": r.get("score", 0.0),
                    "lexical_score": r.get("lexical_score", 0.0),
                    "freshness_score": r.get("freshness_score", 0.0),
                    "confidence_score": r.get("confidence_score", 0.0),
                    "vector_score": r.get("vector_score", 0.0),
                    "breakdown": r.get("breakdown"),
                }
                for r in results if r.get("claim") is not None
            ], cache_generation)
        return results

    def recall_analysis(
        self,
        query_text: str,
        *,
        limit: int = 20,
        retrieval_mode: str = "legacy",
        include_stale: bool = True,
        include_conflicted: bool = True,
        include_candidates: bool = False,
        retrieval_profile: str | None = None,
        allow_sensitive: bool = False,
        scope_allowlist: list[str] | None = None,
        requesting_agent: str | None = None,
        query_type: str | None = None,
    ) -> dict[str, Any]:
        """Explain WHY each claim ranked where it did (observability only).

        Thin wrapper over :meth:`query_rows` that surfaces the per-claim score
        breakdown already attached to ranked rows, the per-component claim
        rankings, and the retrieval weights/profile in force. Does NOT alter
        ranking math or result order — it only reads what the ranker produced.
        """
        rows = self.query_rows(
            query_text=query_text,
            limit=limit,
            retrieval_mode=retrieval_mode,
            include_stale=include_stale,
            include_conflicted=include_conflicted,
            include_candidates=include_candidates,
            retrieval_profile=retrieval_profile,
            allow_sensitive=allow_sensitive,
            scope_allowlist=scope_allowlist,
            requesting_agent=requesting_agent,
            query_type=query_type,
        )
        return {
            "query": query_text,
            "mode": retrieval_mode,
            "profile": retrieval_profile,
            "rows": len(rows),
            "weights": _recall_weights_snapshot(query_type),
            "component_rankings": _recall_component_rankings(rows),
            "results": [_recall_result_entry(row) for row in rows],
        }

    def _rehydrate_cached_rows(self, stubs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Rebuild query_rows result dicts from cached stubs by re-fetching each
        claim. A valid cache hit means no claim changed since write (generation
        gate), so fetched claims are current; archived/missing are skipped."""
        rows: list[dict[str, Any]] = []
        for stub in stubs:
            claim = self.store.get_claim(stub["id"], include_citations=True)
            if claim is None or claim.status == "archived":
                continue
            rows.append({
                "claim": claim,
                "status": claim.status,
                "annotation": self._annotation_for_claim(claim),
                "score": stub.get("score", 0.0),
                "lexical_score": stub.get("lexical_score", 0.0),
                "freshness_score": stub.get("freshness_score", 0.0),
                "confidence_score": stub.get("confidence_score", 0.0),
                "vector_score": stub.get("vector_score", 0.0),
                "breakdown": stub.get("breakdown"),
            })
        return rows

    def _enrich_with_entity_graph(
        self, results: list[dict[str, Any]], query_text: str, limit: int
    ) -> list[dict[str, Any]]:
        """Add entity-related claims to query results via knowledge graph traversal."""
        try:
            from memorymaster.knowledge.entity_graph import EntityGraph
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
        """Record access + feedback for each claim returned by a query.

        On a read-only store (P1 WAL-discipline, spec §2.2) the direct
        UPDATE would raise OperationalError — and the suppress(Exception)
        below would silently eat it, killing the tiering/decay/quality
        signal (the F9 silent-regression). The RO branch instead appends
        spool lines that the steward drain replays through
        record_accesses_batch / FeedbackTracker, so no signal is lost.
        """
        claim_ids = []
        for row in rows:
            claim = row.get("claim")
            if claim is not None:
                claim_ids.append(claim.id)
        if not claim_ids:
            return

        # Rollup telemetry (additive, best-effort): one recall event per
        # query that returned claims, attributed to the surface-supplied
        # source_agent, plus session-level activity when a session is bound.
        # Must never break recall — observability is fire-and-forget.
        self._emit_recall_telemetry()

        if getattr(self.store, "read_only", False):
            self._spool_accesses(claim_ids, query_text)
            return

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
                from memorymaster.govern.feedback import FeedbackTracker
                db_path = str(getattr(self.store, 'db_path', ''))
                if db_path:
                    ft = FeedbackTracker(db_path)
                    ft.ensure_tables()
                    ft.record_retrieval(claim_ids, query_text)
            except Exception:
                pass  # best-effort

    def _emit_recall_telemetry(self) -> None:
        """Bump the recall counter and session activity for a served query.

        Fire-and-forget: a telemetry failure must never break recall, so the
        whole body is suppressed. The counter always fires (labelled
        ``unknown`` when no source_agent is bound); session activity only when
        a session_id has been bound by the surface.
        """
        with contextlib.suppress(Exception):
            observability.bump_recalls_queried(getattr(self, "source_agent", None))
        # getattr guard: a MemoryService built via __new__ (tests, some internal
        # paths) never runs __init__, so source_agent/session_id may be unset.
        # Telemetry must no-op there, never raise into the recall hot path.
        sid = getattr(self, "session_id", None)
        if sid:
            db_path = str(getattr(self.store, "db_path", "") or "")
            if db_path:
                with contextlib.suppress(Exception):
                    from memorymaster.surfaces.session_tracker import SessionTracker

                    SessionTracker(db_path).record_activity(sid, "query")

    def _spool_accesses(self, claim_ids: list[int], query_text: str) -> None:
        """RO-store branch of _record_accesses: spool, don't write.

        Appends ``access`` (+ ``feedback`` when there is a query) envelopes
        to the JSONL spool (spool.py); jobs/spool_drain replays them through
        the exact same sinks the RW path uses (record_accesses_batch,
        FeedbackTracker.record_retrieval). Best-effort like the RW path —
        a spool I/O failure must never break recall.
        """
        db_path = str(getattr(self.store, "db_path", "") or "")
        if not db_path:
            return
        import hashlib

        from memorymaster.core import spool

        query_hash = (
            hashlib.sha1(query_text.encode("utf-8")).hexdigest()[:12]
            if query_text
            else None
        )
        with contextlib.suppress(Exception):
            spool.append(
                db_path, "access", {"claim_ids": claim_ids, "query_hash": query_hash}
            )
            if query_text:
                spool.append(
                    db_path,
                    "feedback",
                    {"claim_ids": claim_ids, "query_text": query_text},
                )

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
        holder: str | None = None,
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
            holder=holder,
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

    def query_claim_paths(
        self,
        claim_id: int | str,
        *,
        edge_type: str | None = None,
        direction: str = "both",
        max_hops: int = 2,
        include_stale: bool = False,
        include_conflicted: bool = False,
        scope_allowlist: list[str] | None = None,
        allow_sensitive: bool = False,
        requesting_agent: str | None = None,
    ) -> list[dict[str, Any]]:
        """BFS path query over ``claim_links`` from a starting claim.

        Answers relational questions an agent asks about a claim:
          - provenance ("what led to X?")  → ``direction="in"``
          - impact     ("what depends on X?") → ``direction="out"``
          - conflict   ("what contradicts X?") → ``edge_type="contradicts"``

        ``claim_id`` accepts an int id OR a human_id string (resolved via the
        store). ``direction`` is ``in`` (incoming edges), ``out`` (outgoing) or
        ``both``. ``edge_type`` filters traversal to a single link type (None =
        all). ``max_hops`` is clamped to ``MAX_CLAIM_PATH_HOPS``. Claims whose
        status is excluded by ``include_stale``/``include_conflicted`` are
        dropped from results (archived/superseded are always excluded). Other
        agents' private claims are dropped via ``_filter_agent_visibility``.

        Returns a list of dicts, one per reachable claim, each with:
          - ``claim``: the full claim as a dict
          - ``depth``: hop distance from the start (>=1)
          - ``edge_chain``: list of link types traversed to reach it
          - ``path``: list of claim ids from start to this claim
          - ``path_confidence``: WEAKEST-LINK roll-up = the MINIMUM claim
            confidence across every claim on the path (start included). A path
            is only as trustworthy as its least-confident hop.

        An orphaned/unknown claim returns ``[]``. Cycles are handled by the
        underlying BFS visited-set. If ``claim_links`` is missing/empty the
        result is simply empty (logged, no crash).
        """
        try:
            start_id = self.store.resolve_claim_id(claim_id)
        except ValueError:
            logger.info("query_claim_paths: unknown claim_id %r", claim_id)
            return []

        hops = max(1, min(int(max_hops), MAX_CLAIM_PATH_HOPS))
        link_types = [edge_type] if edge_type else None
        try:
            raw = self.store.traverse_relationships(
                start_id,
                link_types=link_types,
                max_depth=hops,
                direction=_path_direction_to_traverse(direction),
            )
        except Exception as exc:  # noqa: BLE001 - graceful fallback, never crash a read
            logger.warning("query_claim_paths: traversal failed for %s: %s", start_id, exc)
            return []

        allowed = self._build_query_statuses(include_stale, include_conflicted, include_candidates=True)
        return self._assemble_path_rows(
            raw, start_id, allowed, requesting_agent,
            scope_allowlist=scope_allowlist, allow_sensitive=allow_sensitive,
        )

    def _assemble_path_rows(
        self,
        raw: list[dict[str, Any]],
        start_id: int,
        allowed_statuses: list[str],
        requesting_agent: str | None,
        *,
        scope_allowlist: list[str] | None = None,
        allow_sensitive: bool = False,
    ) -> list[dict[str, Any]]:
        """Filter traversal hits by status + scope + visibility and shape rows.

        Scope + sensitivity gating mirror the regular query path so graph
        traversal from a known claim_id can't surface cross-scope or
        sensitive claim text (audit: claim-paths-scope-gate). ``scope_allowlist``
        None means "no scope restriction" (internal callers); the MCP surface
        always passes an effective allowlist.
        """
        status_set = set(allowed_statuses)
        kept = [
            entry for entry in raw
            if getattr(entry["claim"], "status", None) in status_set
            and _path_claim_in_scope(entry["claim"], scope_allowlist, allow_sensitive)
        ]
        visible = _filter_agent_visibility([e["claim"] for e in kept], requesting_agent)
        visible_ids = {c.id for c in visible}

        start_claim = self.store.get_claim(start_id, include_citations=False)
        conf_by_id: dict[int, float] = {}
        if start_claim is not None:
            conf_by_id[start_id] = float(getattr(start_claim, "confidence", 0.0) or 0.0)
        for entry in kept:
            claim = entry["claim"]
            conf_by_id[claim.id] = float(getattr(claim, "confidence", 0.0) or 0.0)

        rows: list[dict[str, Any]] = []
        for entry in kept:
            claim = entry["claim"]
            if claim.id not in visible_ids:
                continue
            path = entry.get("path", [start_id, claim.id])
            rows.append(
                {
                    "claim": _claim_to_path_dict(claim),
                    "depth": entry["depth"],
                    "edge_chain": _edge_chain_for_path(path, raw, entry),
                    "path": path,
                    "path_confidence": _weakest_link_confidence(path, conf_by_id),
                }
            )
        return rows

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
