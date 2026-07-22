"""Replayable candidate-first Dreaming worker."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.models import (
    ConsolidationResult,
    DreamCandidate,
    DreamDecision,
    ExtractionResult,
    ProviderUsage,
)
from memorymaster.dreaming.providers import GLMConsolidator, GeminiExtractor, ProviderCallError


class Extractor(Protocol):
    provider: str
    model: str

    def extract(self, messages: list[dict[str, Any]], *, scope: str, capture_hash: str) -> ExtractionResult: ...


class Consolidator(Protocol):
    provider: str
    model: str

    def consolidate(self, candidates: list[DreamCandidate], current_claims: list[dict[str, Any]], *, scope: str) -> ConsolidationResult: ...


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class DreamConfig:
    idle_minutes: int = 30
    max_sessions: int = 20
    max_input_chars: int = 128_000
    max_context_chars: int = 512_000
    max_candidate_writes_daily: int = 200
    max_extract_calls_daily: int = 40
    max_consolidate_calls_daily: int = 12
    max_semantic_attempts: int = 2
    lease_ttl_seconds: int = 900
    retain_days: int = 7
    max_capture_bytes: int = 256 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "DreamConfig":
        return cls(
            idle_minutes=_env_int("MEMORYMASTER_DREAM_IDLE_MINUTES", 30),
            max_sessions=_env_int("MEMORYMASTER_DREAM_MAX_SESSIONS", 20),
            max_input_chars=_env_int("MEMORYMASTER_DREAM_MAX_INPUT_CHARS", 128_000),
            max_context_chars=_env_int("MEMORYMASTER_DREAM_MAX_CONTEXT_CHARS", 512_000),
            max_candidate_writes_daily=_env_int("MEMORYMASTER_DREAM_MAX_CANDIDATE_WRITES_DAILY", 200),
            max_extract_calls_daily=_env_int("MEMORYMASTER_DREAM_MAX_EXTRACT_CALLS_DAILY", 40),
            max_consolidate_calls_daily=_env_int("MEMORYMASTER_DREAM_MAX_CONSOLIDATE_CALLS_DAILY", 12),
            max_semantic_attempts=_env_int("MEMORYMASTER_DREAM_MAX_SEMANTIC_ATTEMPTS", 2),
            lease_ttl_seconds=_env_int("MEMORYMASTER_DREAM_LEASE_TTL_SECONDS", 900),
            retain_days=_env_int("MEMORYMASTER_DREAM_CAPTURE_RETAIN_DAYS", 7),
            max_capture_bytes=_env_int("MEMORYMASTER_DREAM_CAPTURE_MAX_BYTES", 256 * 1024 * 1024),
        )


class DreamWorker:
    def __init__(self, ledger: DreamLedger, service: MemoryService, extractor: Extractor, consolidator: Consolidator, *, config: DreamConfig | None = None, now: Callable[[], datetime] | None = None) -> None:
        self.ledger = ledger
        self.service = service
        self.extractor = extractor
        self.consolidator = consolidator
        self.config = config or DreamConfig.from_env()
        self.now = now or (lambda: datetime.now(timezone.utc))

    def run(self, *, apply_candidates: bool, scope: str | None = None, max_sessions: int | None = None) -> dict[str, Any]:
        owner = uuid.uuid4().hex
        if not self.ledger.acquire_lease("dream-worker", owner, self.config.lease_ttl_seconds, now=self.now()):
            return {"ok": False, "reason": "worker_busy"}
        run_id = self.ledger.start_run(not apply_candidates, self.extractor.model, self.consolidator.model, now=self.now())
        summary = {
            "ok": True,
            "run_id": run_id,
            "extracted": 0,
            "consolidated": 0,
            "applied": 0,
            "candidate_writes": 0,
            "proposals": 0,
            "deferred_extract_budget": 0,
            "errors": 0,
        }
        try:
            limit = min(max_sessions or self.config.max_sessions, self.config.max_sessions)
            fresh = self._extract(run_id, scope, limit, summary)
            self._consolidate(run_id, fresh, summary)
            if apply_candidates:
                pending = self.ledger.consolidated(max_sessions=limit, scope=scope)
                self._apply(run_id, pending, summary)
            self.ledger.prune(retain_days=self.config.retain_days, max_bytes=self.config.max_capture_bytes, now=self.now())
            self.ledger.finish_run(run_id, "ok" if not summary["errors"] else "partial", summary, now=self.now())
            return summary
        except Exception as exc:
            summary.update({"ok": False, "errors": int(summary["errors"]) + 1, "fatal": str(exc)[:500]})
            self.ledger.finish_run(run_id, "failed", summary, now=self.now())
            return summary
        finally:
            self.ledger.release_lease("dream-worker", owner)

    def _extract(self, run_id: str, scope: str | None, limit: int, summary: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self.ledger.eligible(idle_minutes=self.config.idle_minutes, max_sessions=limit, scope=scope, now=self.now())
        extracted: list[dict[str, Any]] = []
        for row in rows:
            if row.get("extraction") is not None:
                extracted.append(row)
                continue
            if self.ledger.provider_calls_today(self.extractor.provider, now=self.now()) >= self.config.max_extract_calls_daily:
                summary["deferred_extract_budget"] += 1
                continue
            try:
                messages = self._bounded_messages(list(row["messages"]))
                result = self.extractor.extract(messages, scope=str(row["scope"]), capture_hash=str(row["content_hash"]))
                self._record_usage(run_id, result.usage, "ok")
                candidates = [candidate.to_dict() for candidate in result.candidates]
                self.ledger.set_extraction(int(row["id"]), candidates, run_id)
                row["extraction"] = candidates
                extracted.append(row)
                summary["extracted"] += 1
            except Exception as exc:
                self._record_failure(run_id, self.extractor.provider, self.extractor.model, exc)
                self._mark_extraction_failure(row, run_id, exc)
                summary["errors"] += 1
                if isinstance(exc, ProviderCallError) and exc.http_status == 429:
                    break
        return extracted

    def _mark_extraction_failure(
        self, row: dict[str, Any], run_id: str, error: Exception,
    ) -> None:
        capture_id = int(row["id"])
        attempts_after_failure = int(row.get("attempts", 0)) + 1
        if isinstance(error, ValueError) and attempts_after_failure >= self.config.max_semantic_attempts:
            self.ledger.mark_quarantined(capture_id, run_id, str(error))
            return
        self.ledger.mark_retryable(capture_id, run_id, str(error))

    def _bounded_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        used = 0
        for message in reversed(messages):
            size = len(str(message.get("text", "")))
            if kept and used + size > self.config.max_input_chars:
                break
            kept.append(message)
            used += size
        return list(reversed(kept))

    def _consolidate(self, run_id: str, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
        groups: dict[str, list[tuple[dict[str, Any], DreamCandidate]]] = {}
        for row in rows:
            if row.get("decisions") is not None:
                self.ledger.set_decisions(int(row["id"]), row["decisions"], run_id)
                summary["consolidated"] += 1
                continue
            for payload in row.get("extraction") or []:
                candidate = DreamCandidate(**payload)
                effective_scope = "personal" if candidate.scope_class == "personal" else str(row["scope"])
                groups.setdefault(effective_scope, []).append((row, candidate))
        decisions_by_capture: dict[int, list[dict[str, Any]]] = {int(row["id"]): [] for row in rows}
        failed_captures: set[int] = set()
        for effective_scope in sorted(groups):
            pairs = groups[effective_scope]
            capture_ids = {int(row["id"]) for row, _ in pairs}
            if self.ledger.provider_calls_today(self.consolidator.provider, now=self.now()) >= self.config.max_consolidate_calls_daily:
                self._fail_group(run_id, capture_ids, "consolidate_daily_budget_exhausted", summary)
                failed_captures.update(capture_ids)
                continue
            try:
                candidates = [candidate for _, candidate in pairs]
                result = self.consolidator.consolidate(candidates, self._current_claims(effective_scope), scope=effective_scope)
                self._record_usage(run_id, result.usage, "ok")
                owner_by_id = {candidate.candidate_id: int(row["id"]) for row, candidate in pairs}
                for decision in result.decisions:
                    decisions_by_capture[owner_by_id[decision.candidate_id]].append(decision.to_dict())
            except Exception as exc:
                self._record_failure(
                    run_id, self.consolidator.provider, self.consolidator.model, exc,
                )
                self._fail_group(run_id, capture_ids, str(exc), summary)
                failed_captures.update(capture_ids)
        for row in rows:
            capture_id = int(row["id"])
            if capture_id in failed_captures:
                continue
            self.ledger.set_decisions(capture_id, decisions_by_capture[capture_id], run_id)
            summary["consolidated"] += 1

    def _fail_group(self, run_id: str, capture_ids: set[int], error: str, summary: dict[str, Any]) -> None:
        for capture_id in capture_ids:
            self.ledger.mark_retryable(capture_id, run_id, error)
            summary["errors"] += 1

    def _current_claims(self, scope: str) -> list[dict[str, Any]]:
        claims = [
            *self.service.list_claims(status="confirmed", limit=200, scope_allowlist=[scope]),
            *self.service.list_claims(status="candidate", limit=100, scope_allowlist=[scope]),
        ]
        out: list[dict[str, Any]] = []
        used = 0
        for claim in claims:
            item = {key: getattr(claim, key, None) for key in ("id", "text", "status", "scope", "claim_type", "subject", "predicate", "object_value", "confidence")}
            size = len(json.dumps(item, ensure_ascii=False))
            if out and used + size > self.config.max_context_chars:
                break
            out.append(item)
            used += size
        return out

    def _apply(self, run_id: str, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
        for row in rows:
            try:
                self._apply_capture(run_id, row, summary)
                self.ledger.mark_applied(int(row["id"]), run_id)
                summary["applied"] += 1
            except Exception as exc:
                self.ledger.mark_retryable(int(row["id"]), run_id, str(exc))
                summary["errors"] += 1

    def _apply_capture(self, run_id: str, row: dict[str, Any], summary: dict[str, Any]) -> None:
        candidates = {payload["candidate_id"]: DreamCandidate(**payload) for payload in row.get("extraction") or []}
        for payload in row.get("decisions") or []:
            decision = DreamDecision(**payload)
            candidate = candidates[decision.candidate_id]
            app_key = self._application_key(row, decision)
            if self.ledger.application_exists(app_key):
                continue
            writes_candidate = decision.action in {
                "add", "reinforce", "propose_supersede", "propose_conflict",
            }
            if (
                writes_candidate
                and self.ledger.candidate_writes_today(now=self.now())
                >= self.config.max_candidate_writes_daily
            ):
                raise RuntimeError("candidate_write_daily_budget_exhausted")
            created_claim_id = self._apply_decision(row, candidate, decision, summary)
            self.ledger.record_application(app_key, run_id=run_id, capture_id=int(row["id"]), candidate_id=candidate.candidate_id, action=decision.action, target_claim_id=decision.target_claim_id, created_claim_id=created_claim_id, now=self.now())

    @staticmethod
    def _application_key(row: dict[str, Any], decision: DreamDecision) -> str:
        material = f"{row['id']}|{decision.candidate_id}|{decision.action}|{decision.target_claim_id or ''}"
        return "da-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]

    def _apply_decision(self, row: dict[str, Any], candidate: DreamCandidate, decision: DreamDecision, summary: dict[str, Any]) -> int | None:
        if decision.action == "ignore":
            return None
        effective_scope = "personal" if candidate.scope_class == "personal" else str(row["scope"])
        created = None
        if decision.action in {"add", "reinforce", "propose_supersede", "propose_conflict"}:
            created = self._ingest_candidate(row, candidate, effective_scope)
            summary["candidate_writes"] += 1
        if decision.action.startswith("propose_"):
            self._emit_proposal(candidate, decision, effective_scope, created.id if created else None)
            summary["proposals"] += 1
        return int(created.id) if created else None

    def _ingest_candidate(self, row: dict[str, Any], candidate: DreamCandidate, scope: str):
        identity = hashlib.sha256(f"{scope}|{candidate.candidate_id}".encode("utf-8")).hexdigest()[:24]
        locator = f"dream:{row['provider']}:{row['session_hash'][:12]}:{candidate.evidence_message_id}"
        return self.service.ingest(
            candidate.text,
            [CitationInput(source="dream-worker", locator=locator, excerpt=candidate.evidence_quote)],
            idempotency_key=f"dream-{identity}", claim_type=candidate.claim_type,
            subject=candidate.subject, predicate=candidate.predicate,
            object_value=candidate.object_value, scope=scope, confidence=0.6,
            valid_from=candidate.valid_from, valid_until=candidate.valid_until,
            source_agent="dream-worker", require_source_agent=True,
            intake_batch_id=f"dream-{row['run_id'] or 'replay'}", intake_batch_max=200,
        )

    def _emit_proposal(self, candidate: DreamCandidate, decision: DreamDecision, scope: str, replacement_id: int | None) -> None:
        target = self.service.store.get_claim(int(decision.target_claim_id or 0), include_citations=False)
        if target is None or str(target.scope) != scope:
            raise ValueError("proposal target is missing or outside the candidate scope")
        proposal_decision = {"propose_supersede": "superseded_candidate", "propose_stale": "stale", "propose_conflict": "conflicted"}[decision.action]
        proposed_status = {"superseded_candidate": "superseded", "stale": "stale", "conflicted": "conflicted"}[proposal_decision]
        if self._proposal_exists(target.id, candidate.candidate_id, proposal_decision):
            return
        self.service.store.record_event(
            claim_id=target.id, event_type="policy_decision", from_status=target.status,
            to_status=proposed_status, details=f"steward_proposal:{proposal_decision}",
            payload={"source": "dream-worker", "proposal_type": "review_queue_item",
                     "decision": proposal_decision, "proposed_status": proposed_status,
                     "priority": decision.confidence, "apply_requested": False,
                     "reasons": [{"code": "dream_consolidation", "detail": decision.rationale}],
                     "replaced_by_claim_id": replacement_id, "candidate_id": candidate.candidate_id},
        )

    def _proposal_exists(self, claim_id: int, candidate_id: str, decision: str) -> bool:
        for event in self.service.list_events(claim_id=claim_id, event_type="policy_decision", limit=100):
            try:
                payload = json.loads(event.payload_json or "{}")
            except json.JSONDecodeError:
                continue
            if payload.get("source") == "dream-worker" and payload.get("candidate_id") == candidate_id and payload.get("decision") == decision:
                return True
        return False

    def _record_usage(self, run_id: str, usage: ProviderUsage, outcome: str) -> None:
        self.ledger.record_provider_call(run_id, provider=usage.provider, model=usage.model,
            outcome=outcome, latency_ms=usage.latency_ms, structured_valid=usage.structured_valid,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            http_status=usage.http_status, now=self.now())

    def _record_failure(
        self, run_id: str, provider: str, model: str, error: Exception,
    ) -> None:
        self.ledger.record_provider_call(run_id, provider=provider, model=model,
            outcome="error", latency_ms=0, structured_valid=False, input_tokens=0,
            output_tokens=0, http_status=int(getattr(error, "http_status", 0)), now=self.now())


def run_dream(db_path: str | Path, workspace: str | Path, *, apply_candidates: bool = False, scope: str | None = None, max_sessions: int | None = None, ledger_path: str | Path | None = None) -> dict[str, Any]:
    from memorymaster.core.capture_control import capture_state_path

    ledger = DreamLedger(ledger_path or capture_state_path())
    service = MemoryService(db_path, workspace_root=workspace)
    worker = DreamWorker(ledger, service, GeminiExtractor(), GLMConsolidator())
    return worker.run(apply_candidates=apply_candidates, scope=scope, max_sessions=max_sessions)
