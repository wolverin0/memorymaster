"""Replayable candidate-first Dreaming worker."""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import subprocess
import unicodedata
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from memorymaster.core.antigravity_client import AntigravityError
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.dreaming import held
from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.models import (
    ConsolidationResult,
    DreamCandidate,
    DreamDecision,
    ExtractionResult,
    ProviderUsage,
)
from memorymaster.dreaming.providers import (
    create_dream_consolidator,
    ProviderCallError,
    ProviderConfigError,
    config_failure_reason,
    create_dream_extractor,
)

LOGGER = logging.getLogger(__name__)


class DreamLeaseLost(RuntimeError):
    """Another worker owns the Dreaming lease; this run must stop writing."""


class DreamBudgetDeferred(RuntimeError):
    """A daily budget postponed the work; it says nothing about the capture."""


class DreamPromptTooLarge(RuntimeError):
    """A batch over the consolidator's prompt cap even without references.

    Deterministic, so it counts against its captures like any unexpected
    error; the prompt is never sent or charged.
    """


class DreamProviderStopped(RuntimeError):
    """Ruling R1: a configuration/auth provider failure stops the whole run."""

    def __init__(self, stage: str, provider: str, error: BaseException) -> None:
        super().__init__(f"{stage} provider configuration failure")
        self.stage = stage
        self.provider = provider
        self.reason = _config_reason(error) or "configuration"
        self.detail = str(error)[:300]

    def as_summary(self) -> dict[str, str]:
        return {"stage": self.stage, "provider": self.provider, "reason": self.reason, "detail": self.detail}


# Failures that say nothing about the capture (operator decision 2026-09-23):
# rate limit, auth, request timeout and any 5xx (incl. 529 overload); raised
# bare or underneath a ProviderCallError: timeouts, connection/transport
# errors and every AntigravityError (quota, CLI unavailable, timeout). The
# client's deterministic pre-call prompt cap is never reached: _fit_prompt keeps
# each batch under it or raises DreamPromptTooLarge, which counts (review B1).
_PROVIDER_WIDE_HTTP = frozenset({401, 403, 408, 429})
_TRANSIENT_ERRORS = (TimeoutError, ConnectionError, subprocess.TimeoutExpired, AntigravityError)
_TRANSPORT_CAUSES = (OSError, http.client.HTTPException, subprocess.TimeoutExpired, AntigravityError)


def _config_reason(error: BaseException) -> str | None:
    """Ruling R1 reason code for a configuration/auth failure, else ``None``.

    Missing key, invalid/expired key (Gemini answers HTTP 400), 401, 403, 404
    (model not found), a retired provider or a missing CLI: nothing about the
    capture, and no capture can succeed until the configuration is fixed.
    """
    if isinstance(error, ProviderConfigError):
        return error.reason
    if isinstance(error, ProviderCallError) and not isinstance(error, ValueError):
        return config_failure_reason(int(error.http_status or 0))  # a bare 400 stays capture-level
    return None


def _is_provider_wide(error: BaseException) -> bool:
    """Transient or provider-wide failure; semantic and unexpected errors are not."""
    if isinstance(error, ValueError):  # includes ProviderOutputError
        return False
    if _config_reason(error) is not None:
        return True
    if isinstance(error, _TRANSIENT_ERRORS):
        return True
    if not isinstance(error, ProviderCallError):
        return False
    status = int(error.http_status or 0)
    if status in _PROVIDER_WIDE_HTTP or 500 <= status <= 599:
        return True
    cause, seen = error.__cause__, set()
    while cause is not None and id(cause) not in seen:
        if isinstance(cause, _TRANSPORT_CAUSES):
            return True
        seen.add(id(cause))
        cause = cause.__cause__
    return False


def _failure_reason(error: BaseException) -> str:
    """Short, message-free label for a provider-wide failure (run summary)."""
    status = int(getattr(error, "http_status", 0) or 0)
    if status:
        return f"http_{status}"
    root, seen = error, {id(error)}
    while root.__cause__ is not None and id(root.__cause__) not in seen:
        root = root.__cause__
        seen.add(id(root))
    return type(root).__name__


# Ruling R2 (poison batch): isolate a capture after its consolidation calls
# tripped the breaker in this many consecutive runs, and charge one error per
# run once it alone tripped it in this many consecutive runs.
POISON_ISOLATE_AFTER = 2
POISON_CHARGE_AFTER = 3


def _is_deferral(error: Exception) -> bool:
    # Budget waits and transient/provider-wide failures do not count against a capture.
    return isinstance(error, DreamBudgetDeferred) or _is_provider_wide(error)


def _estimated_tokens(text: str) -> int:
    """Input-token estimate (about four characters per token) for unreported usage."""
    return (len(text) + 3) // 4


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


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class DreamConfig:
    idle_minutes: int = 30
    max_sessions: int = 20
    max_input_chars: int = 128_000
    max_context_chars: int = 512_000
    max_candidate_writes_daily: int = 200
    max_extract_calls_daily: int = 40
    max_consolidate_calls_daily: int = 12
    # TOPE DE GASTO, ordenado por el operador el 2026-08-24 (ruling MM3).
    # Los dos topes de arriba cuentan LLAMADAS, y por eso nunca acotaron el gasto:
    # medido sobre el ledger de produccion, 527 llamadas a openai costaron 9,76M
    # tokens de entrada y 540 a google costaron 3,89M. El mismo tope de llamadas
    # deja pasar gastos que difieren 2,5x, asi que el tope real tiene que estar en
    # tokens. 2M/dia por proveedor es holgado contra el consumo diario observado y
    # aun asi corta una fuga como la de openai antes de que llegue a los 9,76M.
    max_input_tokens_daily: int = 2_000_000
    max_consolidate_candidates: int = 5
    max_semantic_attempts: int = 2
    # Absolute cap on counted failures per capture, any stage; stage success
    # does not reset it. Only semantic and unexpected errors count: budget
    # deferrals and transient/provider-wide failures (_is_provider_wide) do
    # not, except a capture proven to trip the consolidation breaker alone
    # (ruling R2, POISON_CHARGE_AFTER). 0 disables it.
    max_capture_errors: int = 8
    lease_ttl_seconds: int = 900
    retain_days: int = 7
    max_capture_bytes: int = 256 * 1024 * 1024
    # Ruling R7: a capture holding Jev-held candidates is exempt from retention
    # for at most this many days after the hold (never while quarantined).
    held_retain_days: int = held.HELD_RETAIN_DAYS
    enable_graph_observations: bool = False

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
            max_input_tokens_daily=_env_int(
                "MEMORYMASTER_DREAM_MAX_INPUT_TOKENS_DAILY", 2_000_000,
            ),
            max_consolidate_candidates=_env_int(
                "MEMORYMASTER_DREAM_MAX_CONSOLIDATE_CANDIDATES", 5,
            ),
            max_semantic_attempts=_env_int("MEMORYMASTER_DREAM_MAX_SEMANTIC_ATTEMPTS", 2),
            max_capture_errors=_env_int("MEMORYMASTER_DREAM_MAX_CAPTURE_ERRORS", 8),
            lease_ttl_seconds=_env_int("MEMORYMASTER_DREAM_LEASE_TTL_SECONDS", 900),
            retain_days=_env_int("MEMORYMASTER_DREAM_CAPTURE_RETAIN_DAYS", 7),
            max_capture_bytes=_env_int("MEMORYMASTER_DREAM_CAPTURE_MAX_BYTES", 256 * 1024 * 1024),
            held_retain_days=_env_int(held.HELD_RETAIN_DAYS_ENV, held.HELD_RETAIN_DAYS),
            enable_graph_observations=_env_bool(
                "MEMORYMASTER_GRAPH_OBSERVATIONS", False
            ),
        )


class DreamWorker:
    def __init__(self, ledger: DreamLedger, service: MemoryService, extractor: Extractor, consolidator: Consolidator, *, config: DreamConfig | None = None, now: Callable[[], datetime] | None = None, decision_engine: Any = None) -> None:
        self.ledger = ledger
        self.service = service
        self.extractor = extractor
        self.consolidator = consolidator
        self.config = config or DreamConfig.from_env()
        self.now = now or (lambda: datetime.now(timezone.utc))
        # S3 INGEST: None uses the process-wide decisions engine (off by default).
        self.decision_engine = decision_engine
        self._lease_owner: str | None = None

    def _decisions_ledger(self) -> Any:
        """The S3 decisions ledger: the injected engine's, else the configured one (``None``)."""
        return getattr(self.decision_engine, "ledger", None) if self.decision_engine is not None else None

    def _record_verdicts(self, capture_id: int, decisions: list[dict[str, Any]]) -> None:
        """S3 rewards: the consolidator's verdict on each triaged candidate of this capture."""
        if not decisions:
            return
        try:
            notes = held.triage_notes(self.ledger.get_capture(capture_id).get("extraction"))
            verdicts = [(notes[str(d.get("candidate_id"))], d.get("action")) for d in decisions
                        if str(d.get("candidate_id")) in notes]
            if verdicts:
                held.record_verdicts(self._decisions_ledger(), capture_id, verdicts,
                                     observed_at=self.now().isoformat())
        except Exception as exc:  # noqa: BLE001 - telemetry never fails a Dreaming run
            LOGGER.warning("Dreaming S3 verdicts not recorded for capture %s: %s", capture_id, type(exc).__name__)

    def _record_claim_link(self, note: dict[str, Any], claim_id: int) -> None:
        """S3 rewards: tie the created claim to its candidate's decision for the lifecycle joiner."""
        try:
            held.record_claim_link(self._decisions_ledger(), note, claim_id, observed_at=self.now().isoformat())
        except Exception as exc:  # noqa: BLE001 - telemetry never fails an application
            LOGGER.warning("Dreaming S3 claim link not recorded: %s", type(exc).__name__)

    def _record_held_expired(self, notes: list[dict[str, Any]]) -> None:
        """Ruling R7: held candidates pruned with their capture leave ``held_expired`` outcomes."""
        held.record_expired(self._decisions_ledger(), notes, observed_at=self.now().isoformat())

    def _hold_lease(self) -> None:
        """Renew this run's lease before a provider call or a capture write.

        Raises DreamLeaseLost when another worker took over (e.g. a call outlived
        the TTL), so a stalled run never overwrites the new owner's work.
        """
        owner = self._lease_owner
        if owner is not None and not self.ledger.renew_lease(
            "dream-worker", owner, self.config.lease_ttl_seconds, now=self.now(),
        ):
            raise DreamLeaseLost("dream-worker lease is held by another worker")

    def run(self, *, apply_candidates: bool, scope: str | None = None, max_sessions: int | None = None) -> dict[str, Any]:
        owner = uuid.uuid4().hex
        if not self.ledger.acquire_lease("dream-worker", owner, self.config.lease_ttl_seconds, now=self.now()):
            return {"ok": False, "reason": "worker_busy"}
        self._lease_owner = owner
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
            "deferred_consolidate_budget": 0,
            "deferred_consolidate_provider": 0,
            "consolidate_breaker": None,
            "consolidate_breaker_detail": None,
            "consolidate_isolated": 0,
            "consolidate_poison_charged": 0,
            "ingest_triaged": 0,
            "ingest_held": 0,
            "ingest_explored": 0,
            "errors": 0,
            "recovered_stale_runs": 0,
        }
        try:
            summary["recovered_stale_runs"] = self.ledger.abandon_stale_runs(
                run_id, now=self.now(),
            )
            limit = min(max_sessions or self.config.max_sessions, self.config.max_sessions)
            fresh = self._extract(run_id, scope, limit, summary)
            self._consolidate(run_id, fresh, summary)
            if apply_candidates:
                pending = self.ledger.consolidated(max_sessions=limit, scope=scope)
                self._apply(run_id, pending, summary)
            if self.config.enable_graph_observations:
                self._hold_lease()
                summary["graph_observations"] = self._run_graph_observations(
                    owner=owner,
                    scope=scope,
                    synthesize=apply_candidates,
                )
            self._hold_lease()
            self.ledger.prune(retain_days=self.config.retain_days, max_bytes=self.config.max_capture_bytes, now=self.now(),
                              held_retain_days=self.config.held_retain_days, on_held_expired=self._record_held_expired)
            self.ledger.finish_run(run_id, "ok" if not summary["errors"] else "partial", summary, now=self.now())
            return summary
        except DreamLeaseLost:
            summary.update({"ok": False, "errors": int(summary["errors"]) + 1, "reason": "lease_lost"})
            self.ledger.finish_run(run_id, "failed", summary, now=self.now())
            return summary
        except DreamProviderStopped as stop:
            LOGGER.warning("Dreaming run stopped: %s provider %s configuration failure (%s)",
                           stop.stage, stop.provider, stop.reason)
            summary.update({"ok": False, "errors": int(summary["errors"]) + 1, "reason": "provider_config",
                            "provider_config": stop.as_summary()})
            self.ledger.finish_run(run_id, "failed", summary, now=self.now())
            return summary
        except Exception as exc:
            summary.update({"ok": False, "errors": int(summary["errors"]) + 1, "fatal": str(exc)[:500]})
            self.ledger.finish_run(run_id, "failed", summary, now=self.now())
            return summary
        finally:
            self._lease_owner = None
            self.ledger.release_lease("dream-worker", owner)

    def _observation_scope_pairs(self, scope: str | None) -> list[tuple[str, str | None]]:
        params: tuple[Any, ...] = () if scope is None else (scope,)
        clause = "" if scope is None else "AND scope=?"
        with self.service.store.connect() as conn:
            rows = conn.execute(
                f"""SELECT DISTINCT scope, tenant_id FROM claims
                    WHERE status='confirmed'
                      AND COALESCE(claim_type, '') NOT IN
                          ('observation','skill','summary')
                      {clause} ORDER BY scope, tenant_id""",
                params,
            ).fetchall()
        return [(str(row["scope"]), row["tenant_id"]) for row in rows]

    def _observation_llm(self, system: str, prompt: str) -> str:
        """Sintesis de observaciones por el proveedor por defecto.

        Forzaba `MEMORYMASTER_LLM_PROVIDER="opencode"` — el camino GLM que se dio
        de baja el 2026-08-20. El consolidador migro a Antigravity ese mismo dia
        (`providers.py:build_consolidator`) y este call site quedo atras, mandando
        un modelo de Antigravity al proveedor muerto: falla garantizada.

        Se veia en produccion como `opencode: provider call failed code=call_failed`
        dos veces por corrida de dreaming, con `graph_observations.failed=2` al
        lado, y dejaba jobs de sintesis girando en backoff hasta agotar intentos.
        Medido el 2026-08-30: los mismos dos jobs (#11369, #11370) corridos por el
        proveedor por defecto arman componente, llaman y parsean sin error, y los
        dos devuelven decision=emit.

        No se fija proveedor a proposito: el default es el que sigue la migracion,
        asi que la proxima no vuelve a dejar este call site atras.
        """
        from memorymaster.core.llm_provider import call_llm

        return call_llm(system, prompt)

    def _run_graph_observations(
        self, *, owner: str, scope: str | None, synthesize: bool
    ) -> dict[str, int]:
        from memorymaster.knowledge.graph_observation_engine import (
            GraphObservationEngine,
        )
        from memorymaster.knowledge.ontology import load_ontology

        engine = GraphObservationEngine(self.service.store, llm_call=self._observation_llm)
        # "discovery_jobs_enqueued" counts jobs PUT ON THE QUEUE. It was named
        # discovery_queued and read as discoveries; the components/no-support
        # counters below are the ones that say whether anything was found.
        totals = {
            "discovery_jobs_enqueued": 0,
            "components_found": 0,
            "discovery_no_supports": 0,
            "discovery_no_components": 0,
            "synthesis_queued": 0,
            "emitted": 0,
            "failed": 0,
        }
        cycle_hour = self.now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H")
        scope_pairs = self._observation_scope_pairs(scope)
        for target_scope, tenant_id in scope_pairs:
            _job, created = engine.repo.queue_discovery(
                tenant_id=tenant_id,
                scope=target_scope,
                ontology_version=load_ontology().version,
                cycle_hour=cycle_hour,
            )
            totals["discovery_jobs_enqueued"] += int(created)
        for target_scope in sorted({item[0] for item in scope_pairs}):
            discovered = engine.process_discovery(owner=owner, scope=target_scope)
            totals["synthesis_queued"] += discovered.synthesis_queued
            totals["components_found"] += discovered.components_found
            totals["discovery_no_supports"] += discovered.discovery_no_supports
            totals["discovery_no_components"] += discovered.discovery_no_components
            totals["failed"] += discovered.failed
            if synthesize:
                synthesized = engine.process_synthesis(owner=owner, scope=target_scope)
                totals["emitted"] += synthesized.emitted
                totals["failed"] += synthesized.failed
        return totals

    def _extract(self, run_id: str, scope: str | None, limit: int, summary: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self.ledger.eligible(idle_minutes=self.config.idle_minutes, max_sessions=limit, scope=scope, now=self.now())
        extracted: list[dict[str, Any]] = []
        for row in rows:
            if row.get("extraction") is not None:
                extracted.append(row)
                continue
            if self.ledger.provider_calls_today(
                self.extractor.provider,
                model=self.extractor.model,
                now=self.now(),
            ) >= self.config.max_extract_calls_daily or self._over_token_budget(
                self.extractor.provider, self.extractor.model,
            ):
                summary["deferred_extract_budget"] += 1
                continue
            called = False
            messages: list[dict[str, Any]] = []
            try:
                messages = self._bounded_messages(list(row["messages"]))
                self._hold_lease()
                called = True
                result = self.extractor.extract(messages, scope=str(row["scope"]), capture_hash=str(row["content_hash"]))
                called = False
                self._record_usage(run_id, result.usage, "ok")
                candidates = [candidate.to_dict() for candidate in result.candidates]
                self._hold_lease()
                self.ledger.set_extraction(int(row["id"]), candidates, run_id)
                row["extraction"] = candidates
                extracted.append(row)
                summary["extracted"] += 1
            except DreamLeaseLost:
                raise
            except Exception as exc:
                if _config_reason(exc) is not None:
                    # Ruling R1: never charged to the capture; the run stops here.
                    self._record_config_failure(run_id, self.extractor.provider, self.extractor.model, exc)
                    raise DreamProviderStopped("extract", self.extractor.provider, exc) from exc
                if called:
                    self._record_failure(
                        run_id, self.extractor.provider, self.extractor.model, exc,
                        input_tokens=_estimated_tokens(json.dumps(
                            {"scope": str(row["scope"]), "messages": messages}, ensure_ascii=False,
                        )),
                    )
                self._hold_lease()
                self._mark_semantic_failure(row, run_id, exc, stage="extract")
                summary["errors"] += 1
                if isinstance(exc, ProviderCallError) and exc.http_status == 429:
                    break
        return extracted

    def _mark_semantic_failure(
        self, row: dict[str, Any], run_id: str, error: Exception, *, stage: str, charge: bool = False,
    ) -> None:
        """Quarantine repeated validation failures without retrying unsafe output.

        The per-stage semantic count lives in `last_error`; only a stage success
        clears it. Transient errors and budget deferrals keep it unchanged.
        Semantic and unexpected failures (not transient/provider-wide, not a
        budget deferral) also advance the capture's absolute `error_count`,
        which quarantines at `max_capture_errors`. ``charge`` counts a
        provider-wide failure the capture was proven to cause (ruling R2).
        """
        capture_id = int(row["id"])
        # `attempts` counts successful state transitions too. Keep a versioned
        # per-stage semantic counter in existing durable error metadata instead.
        prefix = f"semantic-failure-v1:{stage}:"
        previous = re.match(re.escape(prefix) + r"(0|[1-9][0-9]{0,8}):", str(row.get("last_error") or ""))
        failures = int(previous[1]) if previous else 0
        semantic = isinstance(error, ValueError)
        failures += int(semantic)
        counted = charge or not _is_deferral(error)
        errors = int(row.get("error_count") or 0) + int(counted)
        detail = f"{prefix}{failures}:{error}" if failures else str(error)
        cap = self.config.max_capture_errors
        if (semantic and failures >= self.config.max_semantic_attempts) or (
            counted and cap > 0 and errors >= cap
        ):
            self.ledger.mark_quarantined(capture_id, run_id, detail, error_count=errors)
            return
        self.ledger.mark_retryable(capture_id, run_id, detail, error_count=errors)

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

    def _consolidation_inputs(self, run_id: str, rows: list[dict[str, Any]], summary: dict[str, Any]):
        from memorymaster.dreaming.source_review import REVIEW_VERSION, bind_source

        # Pending work may reuse extraction, never a pre-selection approval.
        rows = [{**row, "decisions": None} if any(
            decision.get("action") != "ignore" and (decision.get("source_review") or {}).get("version") != REVIEW_VERSION
            for decision in row.get("decisions") or []
        ) else row for row in rows]
        rows = [self._reopened(row) for row in rows]
        groups: dict[str, list[tuple[dict[str, Any], DreamCandidate]]] = {}
        for row in rows:
            if row.get("decisions") is not None:
                self._hold_lease()
                self.ledger.set_decisions(int(row["id"]), row["decisions"], run_id,
                                          retry_error=row.get("last_error"))
                summary["consolidated"] += 1
                continue
            payloads = row["released"] if "released" in row else self._triage(row, summary)
            for payload in payloads:
                candidate = DreamCandidate(**held.candidate_fields(payload))
                effective_scope = "personal" if candidate.scope_class == "personal" else str(row["scope"])
                groups.setdefault(effective_scope, []).append((row, bind_source(candidate, row)))
        return rows, groups

    @staticmethod
    def _reopened(row: dict[str, Any]) -> dict[str, Any]:
        """S3: consolidate released candidates of a decided capture, keeping its decisions."""
        released = held.released_undecided(row.get("extraction"), row.get("decisions"))
        if row.get("decisions") is None or not released:
            return row
        return {**row, "decisions": None, "base_decisions": list(row["decisions"]), "released": released}

    def _triage(self, row: dict[str, Any], summary: dict[str, Any]) -> list[dict[str, Any]]:
        """S3 INGEST: ask Jev once per undecided candidate; return what consolidation may see.

        Held candidates stay in ``extraction_json`` (annotated) and are skipped;
        every other outcome, including every fallback, is the legacy path.
        """
        payloads = list(row.get("extraction") or [])
        notes: dict[str, dict[str, Any]] = {}
        for payload in payloads:
            if held.annotation(payload) is None:
                self._hold_lease()
                try:
                    note = self._ingest_decision(row, payload)
                except Exception as exc:  # last resort (no candidate id to log): legacy path, not a failed run
                    LOGGER.warning("Dreaming S3 triage skipped for a candidate of capture %s: %s",
                                   row.get("id"), type(exc).__name__)
                    note = None
                if note is not None:
                    notes[str(payload["candidate_id"])] = note
        if notes:
            def change(current: list[dict[str, Any]]):
                annotated = held.annotate(current, notes)
                return annotated, held.held_count(annotated), False

            self._hold_lease()
            written = self.ledger.update_extraction(int(row["id"]), change)
            payloads = written if written is not None else held.annotate(payloads, notes)
            summary["ingest_triaged"] += len(notes)
            summary["ingest_held"] += sum(note["status"] == held.HELD for note in notes.values())
            summary["ingest_explored"] += sum(note["arm"] == held.EXPLORE_ARM for note in notes.values())
        return [payload for payload in payloads if not held.is_held(payload)]

    def _ingest_decision(self, row: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
        """One S3 decision (batch deadline, budget and breaker live in the engine)."""
        from memorymaster.decisions.engine import DecisionContext, DecisionItem, decide
        from memorymaster.decisions.questions import build_ingest

        ref = held.item_ref(int(row["id"]), str(payload["candidate_id"]))
        scope, features = str(row["scope"]), None
        try:
            candidate = DreamCandidate(**held.candidate_fields(payload))
            scope = "personal" if candidate.scope_class == "personal" else str(row["scope"])
            # Ruling R8: a personal candidate is asked whether it is the operator's own
            # preference, not whether it applies to a project.
            state, questions = build_ingest(candidate.text, candidate.evidence_quote, scope_label=scope, item_ref=ref,
                                            personal=scope == "personal")
            state["excerpt"] = held.capture_excerpt(row.get("messages") or [], candidate)
            features = {"claim_type": candidate.claim_type, "scope_class": candidate.scope_class,
                        "extractor_confidence": candidate.confidence, "capture_provider": row.get("provider")}
        except Exception as exc:
            # Building the request failed outside the engine. An empty request is
            # the engine's own `invalid_request` fallback: legacy admit, nothing
            # sent, and the fallback still reaches the decisions ledger.
            LOGGER.warning("Dreaming S3 request not built for a candidate of capture %s: %s",
                           row.get("id"), type(exc).__name__)
            state, questions = {}, []
        decision = decide(
            "ingest", state=state, questions=questions, items=[DecisionItem(ref, kind=held.ITEM_KIND)],
            legacy_action=held.ADMIT, choose=lambda answers: held.ingest_choice(answers, ref),
            context=DecisionContext(
                kind="batch", scope=scope, tenant=getattr(self.service, "tenant_id", None), exploration="binary",
                baseline_features=features,
            ),
            engine=self.decision_engine,
        )
        if decision.mode == "off":  # nothing asked, nothing logged: leave the extraction untouched
            return None
        return {"status": held.HELD if decision.action == held.HOLD else held.ADMITTED,
                "decision_id": decision.decision_id, "item_ref": ref, "mode": decision.mode,
                "arm": decision.exploration_arm, "fallback_reason": decision.fallback_reason,
                "jev_action": decision.jev_action, "decided_at": self.now().isoformat()}

    @staticmethod
    def _reviewed_batch(result: ConsolidationResult, candidates: list[DreamCandidate], scope: str):
        from memorymaster.dreaming.source_review import reviewed_decision

        by_id = {candidate.candidate_id: candidate for candidate in candidates}
        if len(result.decisions) != len(candidates) or {d.candidate_id for d in result.decisions} != set(by_id):
            raise ValueError("one reviewed decision per candidate is required")
        decisions = [reviewed_decision(by_id[d.candidate_id], d) if d.action != "ignore" else d for d in result.decisions]
        pending = [{"id": None, "status": "pending_reviewed_candidate", "scope": scope,
                    "text": by_id[d.candidate_id].text, "claim_type": by_id[d.candidate_id].claim_type,
                    "memory_key": d.source_review["selection"]["memory_key"]}
                   for d in decisions if d.action == "add"]
        return decisions, pending

    def _consolidate(self, run_id: str, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
        rows, groups = self._consolidation_inputs(run_id, rows, summary)
        decisions_by_capture: dict[int, list[dict[str, Any]]] = {int(row["id"]): [] for row in rows}
        failed_captures: set[int] = set()
        deferred_captures: set[int] = set()
        provider_deferred: set[int] = set()
        completed: set[int] = set()  # in a consolidation call that completed this run
        tripped: set[int] = set()  # in a consolidation call that tripped the breaker this run
        stopped: DreamProviderStopped | None = None
        pending_by_scope: dict[str, list[dict[str, Any]]] = {}
        # Ruling R2: a capture whose calls tripped the breaker in consecutive
        # runs is a poison suspect; it no longer goes first or shares a call.
        streaks = self.ledger.consolidation_trips(int(row["id"]) for row in rows)
        suspects = {capture_id: trips for capture_id, (trips, _) in streaks.items()
                    if trips >= POISON_ISOLATE_AFTER}
        answered = 0  # consolidation calls the provider answered in this run
        batches = deque(
            (effective_scope, batch)
            for effective_scope in sorted(groups)
            for batch in self._candidate_batches(
                [pair for pair in groups[effective_scope] if int(pair[0]["id"]) not in suspects])
        )
        isolated = self._isolated_batches(groups, suspects)
        summary["consolidate_isolated"] = len({int(pairs[0][0]["id"]) for _, pairs in isolated})
        batches.extend(isolated)
        while batches:
            effective_scope, pairs = batches.popleft()
            pairs = [
                pair for pair in pairs if int(pair[0]["id"]) not in failed_captures
            ]
            if not pairs:
                continue
            capture_ids = {int(row["id"]) for row, _ in pairs}
            if stopped is not None or summary["consolidate_breaker"] is not None:
                # Breaker open: no more consolidation calls this run; the rest
                # of the queue stays unchanged for a later run.
                provider_deferred.update(capture_ids)
                summary["deferred_consolidate_provider"] = len(provider_deferred)
                continue
            if self.ledger.provider_calls_today(
                self.consolidator.provider,
                model=self.consolidator.model,
                now=self.now(),
            ) >= self.config.max_consolidate_calls_daily or self._over_token_budget(
                self.consolidator.provider, self.consolidator.model,
            ):
                self._hold_lease()
                for capture_id in capture_ids:
                    self.ledger.defer_consolidation(capture_id, run_id)
                deferred_captures.update(capture_ids)
                summary["deferred_consolidate_budget"] = len(deferred_captures)
                continue
            candidates = [candidate for _, candidate in pairs]
            references: list[dict[str, Any]] = []
            called = False
            try:
                references = [*pending_by_scope.get(effective_scope, []), *self._current_claims(effective_scope, candidates)]
                references = self._fit_prompt(candidates, references, effective_scope)
                self._hold_lease()
                called = True
                result = self.consolidator.consolidate(candidates, references, scope=effective_scope)
                called = False
                answered += 1
                completed.update(capture_ids)
                self._record_usage(run_id, result.usage, "ok")
                owner_by_id = {candidate.candidate_id: int(row["id"]) for row, candidate in pairs}
                decisions, pending = self._reviewed_batch(result, candidates, effective_scope)
                for decision in decisions:
                    decisions_by_capture[owner_by_id[decision.candidate_id]].append(decision.to_dict())
                pending_by_scope[effective_scope] = [*pending_by_scope.get(effective_scope, []), *pending]
            except DreamLeaseLost:
                raise
            except Exception as exc:
                if _config_reason(exc) is not None:
                    # Ruling R1: leave the batch untouched, issue no more calls and
                    # stop the run once the batches already decided are saved.
                    self._record_config_failure(run_id, self.consolidator.provider, self.consolidator.model, exc)
                    stopped = DreamProviderStopped("consolidate", self.consolidator.provider, exc)
                    provider_deferred.update(capture_ids)
                    summary["deferred_consolidate_provider"] = len(provider_deferred)
                    continue
                if called:
                    self._record_failure(
                        run_id, self.consolidator.provider, self.consolidator.model, exc,
                        input_tokens=self._consolidation_tokens(candidates, references, effective_scope),
                    )
                provider_wide = _is_provider_wide(exc)
                charge = False
                if called and not provider_wide:
                    completed.update(capture_ids)  # the call completed: not an outage
                if provider_wide:
                    # Mirror of the extraction 429 break: one provider-wide
                    # failure opens the breaker for the rest of this run.
                    summary["consolidate_breaker"] = _failure_reason(exc)
                    # The label is only the root type; a packed deferral leaves
                    # the captures untouched, so keep the text for diagnosis.
                    summary["consolidate_breaker_detail"] = str(exc)[:300]
                    LOGGER.warning(
                        "Dreaming consolidation breaker open for this run: %s (%s)",
                        summary["consolidate_breaker"], summary["consolidate_breaker_detail"],
                    )
                    if called:
                        # Ruling R2: "alone" = its own call failed although the
                        # provider answered another batch in this run. An
                        # outage (nothing answered) never builds that streak.
                        alone = len(capture_ids) == 1 and answered > 0
                        self._hold_lease()
                        tripped.update(capture_ids)
                        streak = self.ledger.record_consolidation_trip(capture_ids, alone=alone, now=self.now())
                        charge = alone and min((solo for _, solo in streak.values()), default=0) >= POISON_CHARGE_AFTER
                if len(capture_ids) > 1 and provider_wide:
                    # A provider-wide failure says nothing about these captures:
                    # leave the whole batch unchanged for a later run, no fan-out.
                    provider_deferred.update(capture_ids)
                    summary["deferred_consolidate_provider"] = len(provider_deferred)
                    summary["errors"] += 1
                    continue
                if len(capture_ids) > 1:
                    # A packed call failed as a whole: retry each capture alone
                    # (budget permitting) so only the offending capture is charged.
                    batches.extendleft(
                        (effective_scope, capture_pairs)
                        for capture_pairs in reversed(self._by_capture(pairs))
                    )
                    continue
                self._hold_lease()
                self._fail_group(
                    run_id,
                    {int(row["id"]): row for row, _ in pairs},
                    exc,
                    summary,
                    charge=charge,
                )
                summary["consolidate_poison_charged"] += int(charge)
                failed_captures.update(capture_ids)
        for row in rows:
            capture_id = int(row["id"])
            if row.get("decisions") is not None or capture_id in failed_captures or capture_id in deferred_captures or capture_id in provider_deferred:
                continue
            self._hold_lease()
            self.ledger.set_decisions(capture_id, [*row.get("base_decisions", []), *decisions_by_capture[capture_id]],
                                      run_id)
            summary["consolidated"] += 1
            self._record_verdicts(capture_id, decisions_by_capture[capture_id])
        # Ruling R2: a streak ends only when every batch of the capture completed
        # in this run. A capture usually has batches in two effective scopes
        # (`personal` sorts first), so an answered batch followed by a trip, or
        # by a batch the breaker or budget left behind, neither resets it.
        self._clear_trips(completed - tripped - provider_deferred - deferred_captures, streaks)
        if stopped is not None:
            raise stopped

    def _fit_prompt(
        self, candidates: list[DreamCandidate], references: list[dict[str, Any]], scope: str,
    ) -> list[dict[str, Any]]:
        """Keep the longest reference prefix whose prompt fits the consolidator's cap.

        Review B1: the agy client refuses a prompt over its fixed cap before
        sending, and max_context_chars alone can exceed that cap. The refusal
        is an AntigravityError (provider-wide), so the breaker opened on the
        same batch every run and consolidation stalled for every scope.
        References are most relevant first, so the tail is dropped.
        """
        cap = getattr(self.consolidator, "max_prompt_chars", None)
        if not isinstance(cap, int) or cap <= 0:
            return references
        from memorymaster.dreaming.providers import consolidation_prompt

        def size(count: int) -> int:
            return len(consolidation_prompt(candidates, references[:count], scope))

        if size(len(references)) <= cap:
            return references
        bare = size(0)
        if bare > cap:
            raise DreamPromptTooLarge(
                f"consolidation prompt has {bare} characters without reference claims; "
                f"the consolidator cap is {cap}"
            )
        low, high = 0, len(references)  # size(low) fits, size(high) does not
        while high - low > 1:
            middle = (low + high) // 2
            if size(middle) <= cap:
                low = middle
            else:
                high = middle
        LOGGER.info(
            "Dreaming kept %d of %d reference claims to fit the %d-character prompt cap",
            low, len(references), cap,
        )
        return references[:low]

    @staticmethod
    def _consolidation_tokens(
        candidates: list[DreamCandidate], references: list[dict[str, Any]], scope: str,
    ) -> int:
        from memorymaster.dreaming.providers import consolidation_prompt

        try:
            return _estimated_tokens(consolidation_prompt(candidates, references, scope))
        except Exception:  # a prompt that cannot be built was never sent
            return 0

    @staticmethod
    def _by_capture(
        pairs: list[tuple[dict[str, Any], DreamCandidate]],
    ) -> list[list[tuple[dict[str, Any], DreamCandidate]]]:
        groups: list[list[tuple[dict[str, Any], DreamCandidate]]] = []
        for pair in pairs:
            if not groups or groups[-1][0][0]["id"] != pair[0]["id"]:
                groups.append([])
            groups[-1].append(pair)
        return groups

    def _candidate_batches(
        self,
        pairs: list[tuple[dict[str, Any], DreamCandidate]],
    ) -> list[list[tuple[dict[str, Any], DreamCandidate]]]:
        limit = max(1, self.config.max_consolidate_candidates)
        batches: list[list[tuple[dict[str, Any], DreamCandidate]]] = []
        for capture_pairs in self._by_capture(pairs):
            # Pack whole captures up to the candidate limit: every call re-sends
            # the reference context, and one call per capture let the daily call
            # cap strand most of the queue. A packed call that fails for a
            # semantic or unexpected reason is retried per capture in
            # _consolidate (exact failure ownership); a provider-wide failure
            # defers the whole batch unchanged.
            if batches and len(batches[-1]) + len(capture_pairs) <= limit:
                batches[-1].extend(capture_pairs)
            else:
                batches.extend(
                    capture_pairs[index:index + limit]
                    for index in range(0, len(capture_pairs), limit)
                )
        return batches

    def _fail_group(
        self,
        run_id: str,
        rows_by_capture: dict[int, dict[str, Any]],
        error: Exception,
        summary: dict[str, Any],
        *,
        charge: bool = False,
    ) -> None:
        for row in rows_by_capture.values():
            self._mark_semantic_failure(row, run_id, error, stage="consolidate", charge=charge)
            summary["errors"] += 1

    def _isolated_batches(
        self,
        groups: dict[str, list[tuple[dict[str, Any], DreamCandidate]]],
        suspects: dict[int, int],
    ) -> list[tuple[str, list[tuple[dict[str, Any], DreamCandidate]]]]:
        """Ruling R2: one call per suspect capture, fewest consecutive trips first.

        The suspect that just tripped moves behind the others, so one poison
        capture cannot keep the other suspects waiting.
        """
        limit = max(1, self.config.max_consolidate_candidates)
        isolated: list[tuple[str, list[tuple[dict[str, Any], DreamCandidate]]]] = []
        for capture_id in sorted(suspects, key=lambda key: (suspects[key], key)):
            for effective_scope in sorted(groups):
                own = [pair for pair in groups[effective_scope] if int(pair[0]["id"]) == capture_id]
                isolated.extend((effective_scope, own[index:index + limit]) for index in range(0, len(own), limit))
        return isolated

    def _clear_trips(self, capture_ids: set[int], streaks: dict[int, tuple[int, int]]) -> None:
        ids = capture_ids & set(streaks)
        if ids:
            self._hold_lease()
            self.ledger.clear_consolidation_trips(ids)

    def _current_claims(self, scope: str, candidates: list[DreamCandidate] = ()) -> list[dict[str, Any]]:
        from memorymaster.dreaming.source_review import reviewed_memory_keys

        requesting_agent = getattr(self.service, "principal", None) or "dream-worker"
        related = [row["claim"] for candidate in candidates for row in self.service.query_rows(
            candidate.text, limit=20, include_stale=False, include_conflicted=False,
            include_candidates=True, scope_allowlist=[scope], retrieval_mode="legacy", record_accesses=False,
            requesting_agent=requesting_agent,
        )]
        claims = list({claim.id: claim for claim in [
            *related,
            *self.service.list_claims(status="confirmed", limit=200, scope_allowlist=[scope], requesting_agent=requesting_agent),
            *self.service.list_claims(status="candidate", limit=100, scope_allowlist=[scope], requesting_agent=requesting_agent),
        ] if claim.tenant_id == self.service.tenant_id}.values())
        keys = reviewed_memory_keys(self.service.store, [claim.id for claim in claims])
        out: list[dict[str, Any]] = []
        used = 0
        for claim in claims:
            item = {key: getattr(claim, key, None) for key in ("id", "text", "status", "scope", "claim_type", "subject", "predicate", "object_value", "confidence")}
            if claim.id in keys:
                item["memory_key"] = keys[claim.id]
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
                self._hold_lease()
                self.ledger.mark_applied(int(row["id"]), run_id)
                summary["applied"] += 1
            except DreamLeaseLost:
                raise
            except Exception as exc:
                self._hold_lease()
                self._mark_semantic_failure(row, run_id, exc, stage="apply")
                summary["errors"] += 1

    def _apply_capture(self, run_id: str, row: dict[str, Any], summary: dict[str, Any]) -> None:
        candidates = {payload["candidate_id"]: DreamCandidate(**held.candidate_fields(payload))
                      for payload in row.get("extraction") or []}
        triaged = held.triage_notes(row.get("extraction"))
        for payload in row.get("decisions") or []:
            self._hold_lease()
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
                raise DreamBudgetDeferred("candidate_write_daily_budget_exhausted")
            created_claim_id = self._apply_decision(row, candidate, decision, summary)
            actual_action = "ignore" if created_claim_id is None and decision.action != "propose_stale" else decision.action
            self.ledger.record_application(app_key, run_id=run_id, capture_id=int(row["id"]), candidate_id=candidate.candidate_id, action=actual_action, target_claim_id=decision.target_claim_id if actual_action != "ignore" else None, created_claim_id=created_claim_id, now=self.now())
            if created_claim_id is not None and candidate.candidate_id in triaged:
                self._record_claim_link(triaged[candidate.candidate_id], created_claim_id)

    @staticmethod
    def _application_key(row: dict[str, Any], decision: DreamDecision) -> str:
        material = f"{row['id']}|{decision.candidate_id}|{decision.action}|{decision.target_claim_id or ''}"
        return "da-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]

    def _apply_decision(self, row: dict[str, Any], candidate: DreamCandidate, decision: DreamDecision, summary: dict[str, Any]) -> int | None:
        from memorymaster.dreaming.source_review import bind_source, record_review, reviewed_decision

        if decision.action == "ignore":
            return None
        bound = bind_source(candidate, row)
        decision = reviewed_decision(bound, decision)
        if decision.action == "ignore":
            return None
        review = decision.source_review
        effective_scope = "personal" if candidate.scope_class == "personal" else str(row["scope"])
        if decision.action.startswith("propose_"):
            targets = self.service.list_claims(ids=[int(decision.target_claim_id or 0)], limit=1,
                scope_allowlist=[effective_scope], requesting_agent=getattr(self.service, "principal", None) or "dream-worker")
            if not targets or targets[0].tenant_id != self.service.tenant_id:
                raise ValueError("proposal target is missing or outside the candidate scope")
        identity = hashlib.sha256(f"{effective_scope}|{candidate.candidate_id}".encode("utf-8")).hexdigest()[:24]
        previous = self.service.store.get_claim_by_idempotency_key(
            f"dream-{identity}", tenant_id=self.service.tenant_id, scope=effective_scope,
            source_agent="dream-worker",
        )
        if previous is not None and previous.status not in {"candidate", "confirmed"}:
            return None
        if decision.action == "add" and previous is None and self._already_remembered(candidate, effective_scope, review["selection"]["memory_key"]):
            summary["duplicate_ignored"] = summary.get("duplicate_ignored", 0) + 1
            return None
        created = None
        if decision.action in {"add", "reinforce", "propose_supersede", "propose_conflict"}:
            created = self._ingest_candidate(row, candidate, effective_scope)
            if review is not None:
                record_review(self.service.store, created.id, bound, review)
            summary["candidate_writes"] += 1
        if decision.action.startswith("propose_"):
            self._emit_proposal(candidate, decision, effective_scope, created.id if created else None)
            summary["proposals"] += 1
        return int(created.id) if created else None

    def _already_remembered(self, candidate: DreamCandidate, scope: str, memory_key: str) -> bool:
        def normalized(value: str | None) -> str:
            value = unicodedata.normalize("NFKD", str(value or "").casefold())
            return " ".join(re.findall(r"[^\W_]+", "".join(c for c in value if not unicodedata.combining(c))))

        for claim in self._current_claims(scope, [candidate]):
            if claim.get("memory_key") == memory_key.casefold():
                return True
            if normalized(claim["text"]) == normalized(candidate.text):
                return True
            fields = ("subject", "predicate", "object_value")
            if all(normalized(getattr(candidate, key)) and normalized(claim.get(key)) == normalized(getattr(candidate, key))
                   for key in fields):
                return True
        return False

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
        targets = self.service.list_claims(ids=[int(decision.target_claim_id or 0)], limit=1,
            scope_allowlist=[scope], requesting_agent=getattr(self.service, "principal", None) or "dream-worker")
        if not targets or targets[0].tenant_id != self.service.tenant_id:
            raise ValueError("proposal target is missing or outside the candidate scope")
        target = targets[0]
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

    def _over_token_budget(self, provider: str, model: str) -> bool:
        """Tope de gasto por proveedor y dia, en tokens de entrada.

        Se consulta por PROVEEDOR ademas de por modelo: una fuga suele venir de un
        modelo nuevo del mismo proveedor, y un tope por modelo la dejaria pasar
        entera con el contador en cero.

        Un tope de 0 o negativo lo desactiva, para que quede una salida explicita
        y no haya que borrar el cableado si algun dia estorba.
        """
        limite = self.config.max_input_tokens_daily
        if limite <= 0:
            return False
        gastado = self.ledger.provider_input_tokens_today(provider, now=self.now())
        if gastado < limite:
            return False
        LOGGER.warning(
            "Dreaming difiere trabajo: %s ya gasto %d tokens de entrada hoy "
            "(tope %d, modelo %s). Subir MEMORYMASTER_DREAM_MAX_INPUT_TOKENS_DAILY "
            "para ampliarlo.",
            provider, gastado, limite, model,
        )
        return True

    def _record_usage(self, run_id: str, usage: ProviderUsage, outcome: str) -> None:
        self.ledger.record_provider_call(run_id, provider=usage.provider, model=usage.model,
            outcome=outcome, latency_ms=usage.latency_ms, structured_valid=usage.structured_valid,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            http_status=usage.http_status, now=self.now())

    def _record_config_failure(self, run_id: str, provider: str, model: str, error: Exception) -> None:
        """A rejected request is a call but was not billed; a request never sent is no call."""
        if int(getattr(error, "http_status", 0) or 0):
            self._record_failure(run_id, provider, model, error, input_tokens=0)

    def _record_failure(
        self, run_id: str, provider: str, model: str, error: Exception, *, input_tokens: int = 0,
    ) -> None:
        # A failed call can still be billed. Prefer usage the provider reported
        # for rejected output; otherwise charge the request-size estimate so the
        # daily token budget sees the failure.
        reported = int(getattr(error, "input_tokens", 0) or 0)
        self.ledger.record_provider_call(run_id, provider=provider, model=model,
            outcome="error", latency_ms=0, structured_valid=False,
            input_tokens=reported or input_tokens,
            output_tokens=0, http_status=int(getattr(error, "http_status", 0)), now=self.now())


def run_dream(db_path: str | Path, workspace: str | Path, *, apply_candidates: bool = False, scope: str | None = None, max_sessions: int | None = None, ledger_path: str | Path | None = None) -> dict[str, Any]:
    from memorymaster.core.capture_control import capture_state_path

    ledger = DreamLedger(ledger_path or capture_state_path())
    service = MemoryService(db_path, workspace_root=workspace)
    worker = DreamWorker(ledger, service, create_dream_extractor(), create_dream_consolidator())
    return worker.run(apply_candidates=apply_candidates, scope=scope, max_sessions=max_sessions)
