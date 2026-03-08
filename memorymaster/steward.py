from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import json
from pathlib import Path
import subprocess
import time
from typing import Any, Literal

from memorymaster.lifecycle import transition_claim
from memorymaster.security import is_sensitive_claim
from memorymaster.service import MemoryService

StewardMode = Literal["manual", "cadence"]
CadenceTrigger = Literal["timer", "commit", "timer_or_commit"]
DecisionType = Literal["keep", "stale", "conflicted", "superseded_candidate"]
ProbeType = Literal[
    "filesystem_grep",
    "deterministic_format",
    "deterministic_citation_locator",
    "semantic_probe",
    "tool_probe",
]

_TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".rst",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".sql",
    ".csv",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".html",
    ".css",
    ".sh",
    ".ps1",
}
_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".tmp_pytest", ".tmp_cases", "artifacts"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(slots=True)
class Reason:
    code: str
    probe_type: str
    severity: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProbeSpec:
    probe_type: ProbeType
    params: dict[str, Any]


@dataclass(slots=True)
class ClaimProbePlan:
    claim_id: int
    probes: list[ProbeSpec]


@dataclass(slots=True)
class ProbeResult:
    probe_type: ProbeType
    passed: bool
    metrics: dict[str, Any]
    reasons: list[Reason]


@dataclass(slots=True)
class Decision:
    claim_id: int
    current_status: str
    decision: DecisionType
    proposed_status: str | None
    reasons: list[Reason]
    proposal_priority: float
    replaced_by_claim_id: int | None = None
    applied: bool = False
    apply_error: str | None = None


def _normalize_text(value: str | None) -> str:
    return (value or "").strip()


def _status_snapshot(service: MemoryService, *, allow_sensitive: bool, limit: int = 5000) -> dict[str, Any]:
    claims = service.store.list_claims(limit=limit, include_archived=True, include_citations=False)
    if not allow_sensitive:
        claims = [claim for claim in claims if not is_sensitive_claim(claim)]
    counts: dict[str, int] = {}
    for claim in claims:
        counts[claim.status] = counts.get(claim.status, 0) + 1
    return {
        "captured_at": _utc_now(),
        "claim_count": len(claims),
        "pinned_count": sum(1 for claim in claims if bool(claim.pinned)),
        "status_counts": dict(sorted(counts.items())),
    }


def _status_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_counts_raw = before.get("status_counts")
    after_counts_raw = after.get("status_counts")
    before_counts = before_counts_raw if isinstance(before_counts_raw, dict) else {}
    after_counts = after_counts_raw if isinstance(after_counts_raw, dict) else {}
    keys = sorted(set(before_counts.keys()) | set(after_counts.keys()))
    status_count_delta = {
        str(key): int(after_counts.get(key, 0)) - int(before_counts.get(key, 0))
        for key in keys
    }
    return {
        "claim_count_delta": int(after.get("claim_count", 0)) - int(before.get("claim_count", 0)),
        "pinned_count_delta": int(after.get("pinned_count", 0)) - int(before.get("pinned_count", 0)),
        "status_count_delta": status_count_delta,
    }


def _extract_workspace_path_candidate(raw: str | None) -> Path | None:
    text = _normalize_text(raw)
    if not text:
        return None
    lowered = text.lower()
    if lowered.startswith("file://"):
        text = text[7:]
        if text.startswith("/") and len(text) >= 3 and text[2] == ":":
            text = text[1:]
    elif "://" in text:
        return None
    text = text.split("?", 1)[0].split("#", 1)[0].strip()
    if ":" in text:
        if not (len(text) > 1 and text[1] == ":" and ("/" in text or "\\" in text)):
            head, tail = text.rsplit(":", 1)
            if tail.isdigit():
                text = head
    if not text:
        return None
    looks_path = (
        ("/" in text)
        or ("\\" in text)
        or text.startswith(".")
        or Path(text).suffix.lower() in _TEXT_SUFFIXES
    )
    if not looks_path:
        return None
    return Path(text)


def _workspace_display_path(path: Path, workspace_root: Path) -> str:
    try:
        return path.relative_to(workspace_root).as_posix()
    except ValueError:
        return str(path)


def _iter_text_files(workspace_root: Path, *, max_files: int, max_bytes: int) -> list[Path]:
    candidates: list[Path] = []
    for path in sorted(workspace_root.rglob("*"), key=lambda item: str(item).lower()):
        if len(candidates) >= max_files:
            break
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > max_bytes:
                continue
        except OSError:
            continue
        candidates.append(path)
    return candidates


def _run_filesystem_grep_probe(
    *,
    claim: Any,
    workspace_root: Path,
    needle: str,
    max_files: int,
    max_bytes: int,
    max_seconds: float,
) -> ProbeResult:
    reasons: list[Reason] = []
    started = time.monotonic()
    query = needle.strip()
    if not query:
        reasons.append(
            Reason(
                code="filesystem_grep.empty_query",
                probe_type="filesystem_grep",
                severity="low",
                detail="No searchable query text for filesystem probe.",
            )
        )
        return ProbeResult(
            probe_type="filesystem_grep",
            passed=False,
            metrics={
                "query": "",
                "files_scanned": 0,
                "match_count": 0,
                "matched_files": [],
                "budget_exhausted": False,
                "timed_out": False,
                "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
            },
            reasons=reasons,
        )

    files = _iter_text_files(workspace_root, max_files=max_files, max_bytes=max_bytes)
    query_lower = query.lower()
    matched_files: list[str] = []
    files_scanned = 0
    timed_out = False

    for path in files:
        if (time.monotonic() - started) >= max(0.05, float(max_seconds)):
            timed_out = True
            break
        files_scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if query_lower in text.lower():
            rel = path.relative_to(workspace_root).as_posix()
            matched_files.append(rel)

    match_count = len(matched_files)
    budget_exhausted = files_scanned >= max_files

    if match_count == 0:
        reasons.append(
            Reason(
                code="filesystem_grep.no_match",
                probe_type="filesystem_grep",
                severity="medium",
                detail="No filesystem evidence found for claim text/object.",
                evidence={"query": query},
            )
        )
    if budget_exhausted:
        reasons.append(
            Reason(
                code="filesystem_grep.budget_reached",
                probe_type="filesystem_grep",
                severity="low",
                detail="Filesystem probe hit max_files budget.",
                evidence={"max_files": max_files},
            )
        )
    if timed_out:
        reasons.append(
            Reason(
                code="filesystem_grep.timeout",
                probe_type="filesystem_grep",
                severity="medium",
                detail="Filesystem probe timed out before scanning all candidate files.",
                evidence={"max_seconds": max_seconds},
            )
        )

    return ProbeResult(
        probe_type="filesystem_grep",
        passed=match_count > 0,
        metrics={
            "query": query,
            "files_scanned": files_scanned,
            "match_count": match_count,
            "matched_files": matched_files[:10],
            "budget_exhausted": budget_exhausted,
            "timed_out": timed_out,
            "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
        },
        reasons=reasons,
    )


def _run_deterministic_format_probe(*, claim: Any) -> ProbeResult:
    reasons: list[Reason] = []
    started = time.monotonic()
    has_tuple = bool(_normalize_text(claim.subject) and _normalize_text(claim.predicate) and _normalize_text(claim.object_value))
    citation_count = len(claim.citations)
    has_source = all(bool(_normalize_text(citation.source)) for citation in claim.citations)
    has_locator_or_excerpt = all(
        bool(_normalize_text(citation.locator) or _normalize_text(citation.excerpt)) for citation in claim.citations
    ) if claim.citations else False
    text_length = len(_normalize_text(claim.text))

    if not has_tuple:
        reasons.append(
            Reason(
                code="deterministic_format.missing_tuple",
                probe_type="deterministic_format",
                severity="medium",
                detail="Claim does not have a full subject/predicate/object tuple.",
            )
        )
    if citation_count == 0:
        reasons.append(
            Reason(
                code="deterministic_format.no_citations",
                probe_type="deterministic_format",
                severity="high",
                detail="Claim has no citations.",
            )
        )
    if citation_count > 0 and not has_source:
        reasons.append(
            Reason(
                code="deterministic_format.citation_missing_source",
                probe_type="deterministic_format",
                severity="high",
                detail="One or more citations are missing source.",
            )
        )
    if citation_count > 0 and not has_locator_or_excerpt:
        reasons.append(
            Reason(
                code="deterministic_format.weak_citation_context",
                probe_type="deterministic_format",
                severity="low",
                detail="Citations are missing locator/excerpt context.",
            )
        )
    if text_length < 8:
        reasons.append(
            Reason(
                code="deterministic_format.short_text",
                probe_type="deterministic_format",
                severity="low",
                detail="Claim text is very short.",
                evidence={"text_length": text_length},
            )
        )

    passed = not any(reason.severity in {"high", "medium"} for reason in reasons)
    return ProbeResult(
        probe_type="deterministic_format",
        passed=passed,
        metrics={
            "has_tuple": has_tuple,
            "citation_count": citation_count,
            "has_source": has_source,
            "has_locator_or_excerpt": has_locator_or_excerpt,
            "text_length": text_length,
            "timed_out": False,
            "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
        },
        reasons=reasons,
    )


def _run_deterministic_citation_locator_probe(*, claim: Any, workspace_root: Path) -> ProbeResult:
    reasons: list[Reason] = []
    started = time.monotonic()
    candidate_count = 0
    existing_count = 0
    missing_paths: list[str] = []
    existing_paths: list[str] = []

    for citation in claim.citations:
        for field_name, raw_value in (("source", citation.source), ("locator", citation.locator)):
            candidate = _extract_workspace_path_candidate(raw_value)
            if candidate is None:
                continue
            candidate_count += 1
            resolved = candidate if candidate.is_absolute() else (workspace_root / candidate)
            try:
                resolved = resolved.resolve(strict=False)
            except OSError:
                pass
            display = _workspace_display_path(resolved, workspace_root)
            if resolved.exists():
                existing_count += 1
                existing_paths.append(display)
            else:
                missing_paths.append(display)
                reasons.append(
                    Reason(
                        code="deterministic_citation_locator.missing_workspace_path",
                        probe_type="deterministic_citation_locator",
                        severity="medium",
                        detail=f"Citation {field_name} references a missing workspace path.",
                        evidence={"path": display, "field": field_name},
                    )
                )

    return ProbeResult(
        probe_type="deterministic_citation_locator",
        passed=len(missing_paths) == 0,
        metrics={
            "citation_count": len(claim.citations),
            "candidate_path_count": candidate_count,
            "existing_path_count": existing_count,
            "missing_path_count": len(missing_paths),
            "existing_paths": existing_paths[:10],
            "missing_paths": missing_paths[:10],
            "timed_out": False,
            "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
        },
        reasons=reasons,
    )


def _run_semantic_probe(*, claim: Any, service: MemoryService, limit: int, timeout_seconds: float) -> ProbeResult:
    reasons: list[Reason] = []
    started = time.monotonic()
    query = " ".join(
        part
        for part in (
            _normalize_text(claim.subject),
            _normalize_text(claim.predicate),
            _normalize_text(claim.object_value),
            _normalize_text(claim.text)[:80],
        )
        if part
    ).strip()
    if not query:
        reasons.append(
            Reason(
                code="semantic_probe.empty_query",
                probe_type="semantic_probe",
                severity="low",
                detail="No semantic query text available.",
            )
        )
        return ProbeResult(
            probe_type="semantic_probe",
            passed=False,
            metrics={"query": "", "rows": 0, "self_hit": False, "timed_out": False, "duration_ms": 0.0},
            reasons=reasons,
        )

    worker = ThreadPoolExecutor(max_workers=1)
    future = worker.submit(
        service.query,
        query,
        limit=max(1, int(limit)),
        include_stale=True,
        include_conflicted=True,
        retrieval_mode="hybrid",
        allow_sensitive=True,
    )
    timed_out = False
    hits: list[Any] = []
    try:
        hits = future.result(timeout=max(0.05, float(timeout_seconds)))
    except FutureTimeoutError:
        timed_out = True
        future.cancel()
        reasons.append(
            Reason(
                code="semantic_probe.timeout",
                probe_type="semantic_probe",
                severity="medium",
                detail="Semantic probe timed out.",
                evidence={"timeout_seconds": timeout_seconds},
            )
        )
    except Exception as exc:
        reasons.append(
            Reason(
                code="semantic_probe.error",
                probe_type="semantic_probe",
                severity="medium",
                detail=f"Semantic probe failed: {exc}",
            )
        )
    finally:
        worker.shutdown(wait=False, cancel_futures=True)

    self_hit = any(int(getattr(hit, "id", -1)) == int(claim.id) for hit in hits)
    tuple_hit = any(
        _normalize_text(getattr(hit, "subject", None)) == _normalize_text(claim.subject)
        and _normalize_text(getattr(hit, "predicate", None)) == _normalize_text(claim.predicate)
        for hit in hits
    )
    if not timed_out and not self_hit and not tuple_hit:
        reasons.append(
            Reason(
                code="semantic_probe.no_related_hits",
                probe_type="semantic_probe",
                severity="low",
                detail="Semantic probe returned no matching tuple/self hits.",
            )
        )

    return ProbeResult(
        probe_type="semantic_probe",
        passed=(not timed_out) and (self_hit or tuple_hit),
        metrics={
            "query": query,
            "rows": len(hits),
            "self_hit": self_hit,
            "tuple_hit": tuple_hit,
            "timed_out": timed_out,
            "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
        },
        reasons=reasons,
    )


def _run_tool_probe(*, claim: Any, service: MemoryService, timeout_seconds: float) -> ProbeResult:
    reasons: list[Reason] = []
    started = time.monotonic()
    worker = ThreadPoolExecutor(max_workers=1)
    future = worker.submit(service.store.get_claim, int(claim.id), False)
    timed_out = False
    fetched = None
    try:
        fetched = future.result(timeout=max(0.05, float(timeout_seconds)))
    except FutureTimeoutError:
        timed_out = True
        future.cancel()
        reasons.append(
            Reason(
                code="tool_probe.timeout",
                probe_type="tool_probe",
                severity="medium",
                detail="Tool probe timed out while loading claim from storage.",
                evidence={"timeout_seconds": timeout_seconds},
            )
        )
    except Exception as exc:
        reasons.append(
            Reason(
                code="tool_probe.error",
                probe_type="tool_probe",
                severity="medium",
                detail=f"Tool probe failed: {exc}",
            )
        )
    finally:
        worker.shutdown(wait=False, cancel_futures=True)

    exists = fetched is not None
    matches_tuple = (
        exists
        and _normalize_text(fetched.subject) == _normalize_text(claim.subject)
        and _normalize_text(fetched.predicate) == _normalize_text(claim.predicate)
        and _normalize_text(fetched.object_value) == _normalize_text(claim.object_value)
    )
    if not timed_out and not exists:
        reasons.append(
            Reason(
                code="tool_probe.claim_missing",
                probe_type="tool_probe",
                severity="high",
                detail="Claim could not be loaded from storage during tool probe.",
            )
        )
    if exists and not matches_tuple:
        reasons.append(
            Reason(
                code="tool_probe.tuple_mismatch",
                probe_type="tool_probe",
                severity="medium",
                detail="Stored tuple differs from in-memory claim tuple snapshot.",
            )
        )
    return ProbeResult(
        probe_type="tool_probe",
        passed=(not timed_out) and exists and matches_tuple,
        metrics={
            "exists": exists,
            "matches_tuple": matches_tuple,
            "timed_out": timed_out,
            "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
        },
        reasons=reasons,
    )


def plan_claim_probes(
    claim: Any,
    *,
    max_probe_files: int,
    max_file_bytes: int,
    include_semantic_probe: bool = True,
    include_tool_probe: bool = True,
) -> ClaimProbePlan:
    needle = _normalize_text(claim.object_value) or _normalize_text(claim.text)
    if len(needle) > 160:
        needle = needle[:160]
    probes: list[ProbeSpec] = [
        ProbeSpec(
            probe_type="filesystem_grep",
            params={
                "needle": needle,
                "max_files": max_probe_files,
                "max_file_bytes": max_file_bytes,
            },
        ),
        ProbeSpec(probe_type="deterministic_format", params={}),
        ProbeSpec(probe_type="deterministic_citation_locator", params={}),
    ]
    if include_semantic_probe:
        probes.append(ProbeSpec(probe_type="semantic_probe", params={"limit": 8}))
    if include_tool_probe:
        probes.append(ProbeSpec(probe_type="tool_probe", params={}))
    return ClaimProbePlan(claim_id=claim.id, probes=probes)


def _is_newer(left: Any, right: Any) -> bool:
    left_dt = _parse_iso(left.updated_at)
    right_dt = _parse_iso(right.updated_at)
    if left_dt and right_dt and left_dt != right_dt:
        return left_dt > right_dt
    return int(left.id) > int(right.id)


def _claim_key(claim: Any) -> tuple[str, str, str] | None:
    subject = _normalize_text(claim.subject)
    predicate = _normalize_text(claim.predicate)
    scope = _normalize_text(claim.scope) or "project"
    if not subject or not predicate:
        return None
    return (subject, predicate, scope)


def _relation_index(claims: list[Any]) -> dict[tuple[str, str, str], list[Any]]:
    index: dict[tuple[str, str, str], list[Any]] = {}
    for claim in claims:
        key = _claim_key(claim)
        if key is None:
            continue
        index.setdefault(key, []).append(claim)
    for key in list(index.keys()):
        index[key].sort(
            key=lambda item: (_parse_iso(item.updated_at) or datetime.min.replace(tzinfo=timezone.utc), item.id),
            reverse=True,
        )
    return index


def _priority_for_decision(decision: DecisionType, confidence: float) -> float:
    base = {
        "superseded_candidate": 0.9,
        "conflicted": 0.8,
        "stale": 0.6,
        "keep": 0.0,
    }[decision]
    bounded = max(0.0, min(1.0, confidence))
    return round(base + (1.0 - bounded) * 0.1, 6)


def _decision_for_claim(
    *,
    claim: Any,
    probe_results: list[ProbeResult],
    relation_index: dict[tuple[str, str, str], list[Any]],
) -> Decision:
    reasons: list[Reason] = []
    for result in probe_results:
        reasons.extend(result.reasons)

    key = _claim_key(claim)
    replacement: Any | None = None
    has_conflict = False
    if key is not None:
        for peer in relation_index.get(key, []):
            if peer.id == claim.id:
                continue
            if _normalize_text(peer.object_value) == _normalize_text(claim.object_value):
                continue
            if peer.status == "confirmed":
                has_conflict = True
                if _is_newer(peer, claim):
                    replacement = peer
                    break

    decision: DecisionType = "keep"
    proposed_status: str | None = None
    replaced_by_claim_id: int | None = None

    if replacement is not None:
        decision = "superseded_candidate"
        proposed_status = "superseded"
        replaced_by_claim_id = int(replacement.id)
        reasons.append(
            Reason(
                code="relation.newer_confirmed_claim",
                probe_type="relation",
                severity="high",
                detail="Newer confirmed claim exists for same tuple with different object value.",
                evidence={"replaced_by_claim_id": replacement.id},
            )
        )
    elif claim.status == "conflicted" or has_conflict:
        decision = "conflicted"
        proposed_status = "conflicted"
        reasons.append(
            Reason(
                code="relation.conflicting_claim",
                probe_type="relation",
                severity="high",
                detail="Conflicting confirmed claim exists for same tuple.",
            )
        )
    else:
        no_match = any(reason.code == "filesystem_grep.no_match" for reason in reasons)
        weak_format = any(
            reason.code in {
                "deterministic_format.no_citations",
                "deterministic_format.missing_tuple",
                "deterministic_citation_locator.missing_workspace_path",
            }
            for reason in reasons
        )
        if claim.status == "stale" or no_match or weak_format:
            decision = "stale"
            proposed_status = "stale"

    return Decision(
        claim_id=int(claim.id),
        current_status=str(claim.status),
        decision=decision,
        proposed_status=proposed_status,
        reasons=reasons,
        proposal_priority=_priority_for_decision(decision, float(claim.confidence)),
        replaced_by_claim_id=replaced_by_claim_id,
    )


def _apply_decision(service: MemoryService, claim: Any, decision: Decision) -> None:
    if decision.decision == "keep":
        return
    if decision.decision == "stale":
        if claim.status == "stale":
            return
        transition_claim(
            service.store,
            claim_id=claim.id,
            to_status="stale",
            reason="steward_apply:stale",
            event_type="transition",
        )
        decision.applied = True
        return
    if decision.decision == "conflicted":
        if claim.status == "conflicted":
            return
        transition_claim(
            service.store,
            claim_id=claim.id,
            to_status="conflicted",
            reason="steward_apply:conflicted",
            event_type="transition",
        )
        decision.applied = True
        return
    if decision.decision == "superseded_candidate":
        if decision.replaced_by_claim_id is None or claim.status == "superseded":
            return
        service.store.mark_superseded(
            old_claim_id=claim.id,
            new_claim_id=decision.replaced_by_claim_id,
            reason="steward_apply:superseded_candidate",
        )
        decision.applied = True


def _emit_proposal_event(service: MemoryService, claim: Any, decision: Decision, *, apply: bool) -> None:
    payload = {
        "source": "steward",
        "proposal_type": "review_queue_item",
        "decision": decision.decision,
        "proposed_status": decision.proposed_status,
        "priority": decision.proposal_priority,
        "apply_requested": apply,
        "reasons": [asdict(reason) for reason in decision.reasons],
        "replaced_by_claim_id": decision.replaced_by_claim_id,
    }
    service.store.record_event(
        claim_id=claim.id,
        event_type="policy_decision",
        from_status=claim.status,
        to_status=decision.proposed_status,
        details=f"steward_proposal:{decision.decision}",
        payload=payload,
    )


def _run_cycle(
    service: MemoryService,
    *,
    allow_sensitive: bool,
    apply: bool,
    max_claims: int,
    max_proposals: int,
    max_probe_files: int,
    max_probe_file_bytes: int,
    max_tool_probes: int,
    probe_timeout_seconds: float,
    probe_failure_threshold: int,
    enable_semantic_probe: bool,
    enable_tool_probe: bool,
) -> dict[str, Any]:
    before = _status_snapshot(service, allow_sensitive=allow_sensitive)

    claims = service.store.list_claims(
        status_in=["confirmed", "stale", "conflicted"],
        include_archived=False,
        include_citations=True,
        limit=max(max_claims * 5, max_claims),
    )
    if not allow_sensitive:
        claims = [claim for claim in claims if not is_sensitive_claim(claim)]
    claims.sort(
        key=lambda claim: (_parse_iso(claim.updated_at) or datetime.min.replace(tzinfo=timezone.utc), claim.id),
    )
    selected_claims = claims[:max_claims]
    relation_index = _relation_index(claims)

    decisions: list[dict[str, Any]] = []
    proposals_emitted = 0
    proposal_budget_blocks = 0
    applied_count = 0
    probe_failures: dict[str, int] = {}
    circuit_open: set[str] = set()
    circuit_open_count = 0
    tool_probes_executed = 0
    tool_probe_budget_skips = 0
    probe_execution: dict[str, dict[str, int]] = {}

    def _probe_stats_row(probe_type: str) -> dict[str, int]:
        return probe_execution.setdefault(
            probe_type,
            {
                "planned": 0,
                "executed": 0,
                "skipped_circuit_open": 0,
                "skipped_budget": 0,
                "timed_out": 0,
                "errored": 0,
            },
        )

    for claim in selected_claims:
        plan = plan_claim_probes(
            claim,
            max_probe_files=max_probe_files,
            max_file_bytes=max_probe_file_bytes,
            include_semantic_probe=enable_semantic_probe,
            include_tool_probe=enable_tool_probe,
        )
        probe_results: list[ProbeResult] = []
        for probe in plan.probes:
            _probe_stats_row(probe.probe_type)["planned"] += 1
            if probe.probe_type in circuit_open:
                circuit_open_count += 1
                _probe_stats_row(probe.probe_type)["skipped_circuit_open"] += 1
                probe_results.append(
                    ProbeResult(
                        probe_type=probe.probe_type,
                        passed=False,
                        metrics={"circuit_open": True, "timed_out": False, "duration_ms": 0.0},
                        reasons=[
                            Reason(
                                code="probe.circuit_open",
                                probe_type=probe.probe_type,
                                severity="low",
                                detail="Probe skipped because circuit breaker is open.",
                                evidence={
                                    "failure_threshold": probe_failure_threshold,
                                    "failures": probe_failures.get(probe.probe_type, 0),
                                },
                            )
                        ],
                    )
                )
                continue

            if probe.probe_type == "tool_probe" and tool_probes_executed >= max_tool_probes:
                tool_probe_budget_skips += 1
                _probe_stats_row(probe.probe_type)["skipped_budget"] += 1
                probe_results.append(
                    ProbeResult(
                        probe_type=probe.probe_type,
                        passed=False,
                        metrics={
                            "budget_skipped": True,
                            "timed_out": False,
                            "duration_ms": 0.0,
                            "max_tool_probes": max_tool_probes,
                            "tool_probes_executed": tool_probes_executed,
                        },
                        reasons=[
                            Reason(
                                code="budget.max_tool_probes_reached",
                                probe_type=probe.probe_type,
                                severity="medium",
                                detail="Tool probe skipped due to max_tool_probes guardrail.",
                                evidence={
                                    "max_tool_probes": max_tool_probes,
                                    "tool_probes_executed": tool_probes_executed,
                                },
                            )
                        ],
                    )
                )
                continue

            result: ProbeResult | None = None
            if probe.probe_type == "filesystem_grep":
                result = _run_filesystem_grep_probe(
                    claim=claim,
                    workspace_root=service.workspace_root,
                    needle=str(probe.params.get("needle", "")),
                    max_files=int(probe.params.get("max_files", max_probe_files)),
                    max_bytes=int(probe.params.get("max_file_bytes", max_probe_file_bytes)),
                    max_seconds=probe_timeout_seconds,
                )
            elif probe.probe_type == "deterministic_format":
                result = _run_deterministic_format_probe(claim=claim)
            elif probe.probe_type == "deterministic_citation_locator":
                result = _run_deterministic_citation_locator_probe(
                    claim=claim,
                    workspace_root=service.workspace_root,
                )
            elif probe.probe_type == "semantic_probe":
                result = _run_semantic_probe(
                    claim=claim,
                    service=service,
                    limit=int(probe.params.get("limit", 8)),
                    timeout_seconds=probe_timeout_seconds,
                )
            elif probe.probe_type == "tool_probe":
                tool_probes_executed += 1
                result = _run_tool_probe(
                    claim=claim,
                    service=service,
                    timeout_seconds=probe_timeout_seconds,
                )

            if result is None:
                continue
            probe_results.append(result)
            _probe_stats_row(result.probe_type)["executed"] += 1
            timed_out = bool(result.metrics.get("timed_out"))
            had_error = any(reason.code.endswith(".error") for reason in result.reasons)
            if timed_out:
                _probe_stats_row(result.probe_type)["timed_out"] += 1
            if had_error:
                _probe_stats_row(result.probe_type)["errored"] += 1
            if timed_out or had_error:
                failures = int(probe_failures.get(result.probe_type, 0)) + 1
                probe_failures[result.probe_type] = failures
                if failures >= probe_failure_threshold:
                    circuit_open.add(result.probe_type)
            else:
                probe_failures[result.probe_type] = 0

        decision = _decision_for_claim(claim=claim, probe_results=probe_results, relation_index=relation_index)
        if decision.decision != "keep" and proposals_emitted < max_proposals:
            _emit_proposal_event(service, claim, decision, apply=apply)
            proposals_emitted += 1
        elif decision.decision != "keep" and proposals_emitted >= max_proposals:
            proposal_budget_blocks += 1
            decision.reasons.append(
                Reason(
                    code="budget.max_proposals_reached",
                    probe_type="budget",
                    severity="low",
                    detail="Proposal event not emitted due to max_proposals guardrail.",
                )
            )

        if apply:
            try:
                _apply_decision(service, claim, decision)
            except Exception as exc:
                decision.apply_error = str(exc)
            if decision.applied:
                applied_count += 1

        decisions.append(
            {
                "claim_id": decision.claim_id,
                "current_status": decision.current_status,
                "decision": decision.decision,
                "proposed_status": decision.proposed_status,
                "proposal_priority": decision.proposal_priority,
                "replaced_by_claim_id": decision.replaced_by_claim_id,
                "reasons": [asdict(reason) for reason in decision.reasons],
                "probes": [asdict(result) for result in probe_results],
                "applied": decision.applied,
                "apply_error": decision.apply_error,
            }
        )

    after = _status_snapshot(service, allow_sensitive=allow_sensitive)
    delta = _status_delta(before, after)
    probe_totals = {
        "planned": sum(row["planned"] for row in probe_execution.values()),
        "executed": sum(row["executed"] for row in probe_execution.values()),
        "skipped_circuit_open": sum(row["skipped_circuit_open"] for row in probe_execution.values()),
        "skipped_budget": sum(row["skipped_budget"] for row in probe_execution.values()),
        "timed_out": sum(row["timed_out"] for row in probe_execution.values()),
        "errored": sum(row["errored"] for row in probe_execution.values()),
    }
    probe_totals["skipped"] = probe_totals["skipped_circuit_open"] + probe_totals["skipped_budget"]
    max_claims_reached = len(claims) > max_claims
    max_proposals_reached = proposal_budget_blocks > 0
    max_tool_probes_reached = tool_probe_budget_skips > 0
    guardrail_events: list[dict[str, Any]] = []
    if max_claims_reached:
        guardrail_events.append(
            {
                "guardrail": "max_claims",
                "hits": 1,
                "limit": max_claims,
                "seen": len(claims),
            }
        )
    if max_proposals_reached:
        guardrail_events.append(
            {
                "guardrail": "max_proposals",
                "hits": proposal_budget_blocks,
                "limit": max_proposals,
                "emitted": proposals_emitted,
            }
        )
    if max_tool_probes_reached:
        guardrail_events.append(
            {
                "guardrail": "max_tool_probes",
                "hits": tool_probe_budget_skips,
                "limit": max_tool_probes,
                "executed": tool_probes_executed,
            }
        )
    if circuit_open_count > 0:
        guardrail_events.append(
            {
                "guardrail": "probe_circuit_open",
                "hits": circuit_open_count,
                "opened_probe_types": sorted(circuit_open),
            }
        )
    return {
        "before": before,
        "after": after,
        "delta": delta,
        "decisions": decisions,
        "budget": {
            "max_claims": max_claims,
            "max_proposals": max_proposals,
            "max_probe_files": max_probe_files,
            "max_tool_probes": max_tool_probes,
            "claims_scanned": len(selected_claims),
            "proposals_emitted": proposals_emitted,
            "proposals_suppressed": proposal_budget_blocks,
            "applied_changes": applied_count,
            "tool_probes_executed": tool_probes_executed,
            "tool_probes_skipped": tool_probe_budget_skips,
            "probe_execution": probe_execution,
            "accounting": {
                "claims": {
                    "limit": max_claims,
                    "used": len(selected_claims),
                    "remaining": max(0, max_claims - len(selected_claims)),
                    "seen_candidates": len(claims),
                },
                "proposals": {
                    "limit": max_proposals,
                    "used": proposals_emitted,
                    "remaining": max(0, max_proposals - proposals_emitted),
                    "suppressed": proposal_budget_blocks,
                },
                "tool_probes": {
                    "limit": max_tool_probes,
                    "used": tool_probes_executed,
                    "remaining": max(0, max_tool_probes - tool_probes_executed),
                    "suppressed": tool_probe_budget_skips,
                },
                "probes": probe_totals,
                "guardrail_events": guardrail_events,
            },
            "guardrails": {
                "max_claims_reached": max_claims_reached,
                "max_proposals_reached": max_proposals_reached,
                "max_tool_probes_reached": max_tool_probes_reached,
                "probe_timeout_seconds": probe_timeout_seconds,
                "probe_failure_threshold": probe_failure_threshold,
                "probe_circuit_open": sorted(circuit_open),
                "probe_circuit_open_count": circuit_open_count,
                "guardrail_events": guardrail_events,
            },
        },
    }


def _write_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _parse_payload_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def list_steward_proposals(
    service: MemoryService,
    *,
    limit: int = 100,
    include_resolved: bool = False,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    proposal_scan_limit = max(limit * 6, 500)
    proposals_raw = service.list_events(event_type="policy_decision", limit=proposal_scan_limit)
    audit_raw = service.list_events(event_type="audit", limit=max(limit * 8, 800))

    resolution_by_proposal_event_id: dict[int, dict[str, Any]] = {}
    for event in audit_raw:
        details = str(event.details or "")
        if details not in {"steward_proposal_approved", "steward_proposal_rejected"}:
            continue
        payload = _parse_payload_json(event.payload_json)
        proposal_event_id = payload.get("proposal_event_id")
        if isinstance(proposal_event_id, int):
            resolution_by_proposal_event_id[proposal_event_id] = {
                "status": "approved" if details.endswith("approved") else "rejected",
                "resolved_event_id": int(event.id),
                "resolved_at": event.created_at,
                "resolved_claim_id": event.claim_id,
                "details": details,
                "payload": payload,
            }

    out: list[dict[str, Any]] = []
    for event in proposals_raw:
        details = str(event.details or "")
        if not details.startswith("steward_proposal:"):
            continue
        payload = _parse_payload_json(event.payload_json)
        proposal_status = "pending"
        resolution = resolution_by_proposal_event_id.get(int(event.id))
        if resolution is not None:
            proposal_status = str(resolution["status"])
        if not include_resolved and proposal_status != "pending":
            continue
        out.append(
            {
                "proposal_event_id": int(event.id),
                "claim_id": event.claim_id,
                "created_at": event.created_at,
                "proposal_decision": payload.get("decision") or details.split(":", 1)[-1],
                "proposed_status": payload.get("proposed_status"),
                "priority": payload.get("priority"),
                "replaced_by_claim_id": payload.get("replaced_by_claim_id"),
                "reasons": payload.get("reasons") if isinstance(payload.get("reasons"), list) else [],
                "status": proposal_status,
                "resolution": resolution,
                "payload": payload,
            }
        )
        if len(out) >= limit:
            break
    return out


def resolve_steward_proposal(
    service: MemoryService,
    *,
    action: Literal["approve", "reject"],
    proposal_event_id: int | None = None,
    claim_id: int | None = None,
    apply_on_approve: bool = True,
) -> dict[str, Any]:
    if action not in {"approve", "reject"}:
        raise ValueError("action must be approve or reject")
    if proposal_event_id is None and claim_id is None:
        raise ValueError("proposal_event_id or claim_id is required")

    proposals = list_steward_proposals(service, limit=2000, include_resolved=True)
    target: dict[str, Any] | None = None
    if proposal_event_id is not None:
        target = next((item for item in proposals if int(item["proposal_event_id"]) == int(proposal_event_id)), None)
    else:
        pending_for_claim = [
            item for item in proposals if item["claim_id"] == claim_id and str(item.get("status")) == "pending"
        ]
        if pending_for_claim:
            target = sorted(pending_for_claim, key=lambda item: int(item["proposal_event_id"]), reverse=True)[0]
        else:
            any_for_claim = [item for item in proposals if item["claim_id"] == claim_id]
            if any_for_claim:
                target = sorted(any_for_claim, key=lambda item: int(item["proposal_event_id"]), reverse=True)[0]
    if target is None:
        raise ValueError("No steward proposal found for requested selector.")

    if str(target.get("status")) in {"approved", "rejected"}:
        return {
            "ok": True,
            "resolved": False,
            "reason": "already_resolved",
            "proposal": target,
        }

    target_claim_id = target.get("claim_id")
    if not isinstance(target_claim_id, int) or target_claim_id <= 0:
        raise ValueError("Selected proposal has invalid claim_id.")

    payload = target.get("payload") if isinstance(target.get("payload"), dict) else {}
    decision = str(payload.get("decision") or target.get("proposal_decision") or "").strip().lower()
    proposed_status = str(payload.get("proposed_status") or target.get("proposed_status") or "").strip().lower()
    replaced_by_claim_id = payload.get("replaced_by_claim_id")
    applied = False
    apply_error: str | None = None

    if action == "approve" and apply_on_approve:
        claim = service.store.get_claim(target_claim_id, include_citations=False)
        if claim is None:
            raise ValueError(f"Claim {target_claim_id} does not exist.")
        try:
            if decision == "superseded_candidate" and isinstance(replaced_by_claim_id, int):
                service.store.mark_superseded(
                    old_claim_id=target_claim_id,
                    new_claim_id=int(replaced_by_claim_id),
                    reason="steward_human_override:approve",
                )
                applied = True
            elif proposed_status in {"stale", "conflicted", "superseded"}:
                transition_claim(
                    service.store,
                    claim_id=target_claim_id,
                    to_status=proposed_status,  # type: ignore[arg-type]
                    reason="steward_human_override:approve",
                    event_type="transition",
                    replaced_by_claim_id=(int(replaced_by_claim_id) if isinstance(replaced_by_claim_id, int) else None),
                )
                applied = True
        except Exception as exc:  # pragma: no cover
            apply_error = str(exc)

    audit_details = "steward_proposal_approved" if action == "approve" else "steward_proposal_rejected"
    audit_payload: dict[str, Any] = {
        "source": "human_override",
        "proposal_event_id": int(target["proposal_event_id"]),
        "proposal_decision": decision or None,
        "proposed_status": proposed_status or None,
        "apply_on_approve": bool(apply_on_approve),
        "applied": applied,
        "apply_error": apply_error,
    }
    service.store.record_event(
        claim_id=target_claim_id,
        event_type="audit",
        details=audit_details,
        payload=audit_payload,
    )

    return {
        "ok": True,
        "resolved": True,
        "action": action,
        "proposal_event_id": int(target["proposal_event_id"]),
        "claim_id": target_claim_id,
        "applied": applied,
        "apply_error": apply_error,
        "status": ("approved" if action == "approve" else "rejected"),
    }


def _get_git_head(workspace_root: Path) -> str | None:
    resolved = workspace_root.resolve()
    try:
        proc = subprocess.run(
            ["git", "-C", str(resolved), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    head = proc.stdout.strip()
    if not head or len(head) != 40 or not all(c in "0123456789abcdef" for c in head):
        return None
    return head


def _wait_for_cadence_trigger(
    *,
    workspace_root: Path,
    cadence_trigger: CadenceTrigger,
    interval_seconds: float,
    git_check_seconds: float,
    commit_every: int,
    next_due_monotonic: float | None,
    last_head: str | None,
    commits_since_cycle: int,
    total_commit_observations: int,
    git_unavailable_polls: int,
) -> dict[str, Any]:
    if cadence_trigger not in {"commit", "timer_or_commit"}:
        raise ValueError("cadence_trigger must be commit or timer_or_commit")
    started = time.monotonic()
    polls = 0
    next_git_check = time.monotonic()
    current_last_head = last_head
    commits_seen = commits_since_cycle
    total_commits = total_commit_observations
    unavailable_polls = git_unavailable_polls
    next_due = next_due_monotonic
    while True:
        now = time.monotonic()
        timer_due = (
            cadence_trigger == "timer_or_commit"
            and next_due is not None
            and now >= next_due
        )
        if timer_due:
            return {
                "trigger": "timer",
                "waited_seconds": round(max(0.0, now - started), 3),
                "polls": polls,
                "git_head": current_last_head,
                "commits_since_cycle": commits_seen,
                "total_commit_observations": total_commits,
                "git_unavailable_polls": unavailable_polls,
            }
        if now >= next_git_check:
            polls += 1
            next_git_check = now + max(0.25, float(git_check_seconds))
            head = _get_git_head(workspace_root)
            if head is None:
                unavailable_polls += 1
            else:
                if current_last_head is not None and head != current_last_head:
                    commits_seen += 1
                    total_commits += 1
                current_last_head = head
            if commits_seen >= commit_every:
                return {
                    "trigger": "commit",
                    "waited_seconds": round(max(0.0, now - started), 3),
                    "polls": polls,
                    "git_head": current_last_head,
                    "commits_since_cycle": commits_seen,
                    "total_commit_observations": total_commits,
                    "git_unavailable_polls": unavailable_polls,
                }
        time.sleep(0.25)


def run_steward(
    service: MemoryService,
    *,
    mode: StewardMode = "manual",
    cadence_trigger: CadenceTrigger = "timer",
    interval_seconds: float = 30.0,
    git_check_seconds: float = 10.0,
    commit_every: int = 1,
    max_cycles: int = 1,
    allow_sensitive: bool = False,
    apply: bool = False,
    max_claims: int = 200,
    max_proposals: int = 200,
    max_probe_files: int = 200,
    max_probe_file_bytes: int = 512 * 1024,
    max_tool_probes: int = 200,
    probe_timeout_seconds: float = 2.0,
    probe_failure_threshold: int = 3,
    enable_semantic_probe: bool = True,
    enable_tool_probe: bool = True,
    artifact_path: Path | str = "artifacts/steward/steward_report.json",
) -> dict[str, Any]:
    if mode not in {"manual", "cadence"}:
        raise ValueError("mode must be one of: manual, cadence")
    if cadence_trigger not in {"timer", "commit", "timer_or_commit"}:
        raise ValueError("cadence_trigger must be one of: timer, commit, timer_or_commit")
    if max_cycles <= 0:
        raise ValueError("max_cycles must be > 0")
    if max_claims <= 0:
        raise ValueError("max_claims must be > 0")
    if max_proposals < 0:
        raise ValueError("max_proposals must be >= 0")
    if max_probe_files <= 0:
        raise ValueError("max_probe_files must be > 0")
    if max_probe_file_bytes <= 0:
        raise ValueError("max_probe_file_bytes must be > 0")
    if max_tool_probes < 0:
        raise ValueError("max_tool_probes must be >= 0")
    if probe_timeout_seconds <= 0:
        raise ValueError("probe_timeout_seconds must be > 0")
    if probe_failure_threshold <= 0:
        raise ValueError("probe_failure_threshold must be > 0")
    if commit_every <= 0:
        raise ValueError("commit_every must be > 0")
    if git_check_seconds <= 0:
        raise ValueError("git_check_seconds must be > 0")

    started_at = _utc_now()
    started_monotonic = time.monotonic()
    run_id = f"steward-{time.time_ns()}"
    cycles: list[dict[str, Any]] = []
    trigger_counts: dict[str, int] = {"manual": 0, "timer": 0, "commit": 0}
    commit_trigger_enabled = mode == "cadence" and cadence_trigger in {"commit", "timer_or_commit"}
    initial_git_head = _get_git_head(service.workspace_root) if commit_trigger_enabled else None
    last_git_head = initial_git_head
    commits_since_cycle = 0
    total_commit_observations = 0
    git_unavailable_polls = 0
    next_due_monotonic = time.monotonic() if cadence_trigger == "timer_or_commit" else None

    for cycle_index in range(1, max_cycles + 1):
        trigger_kind = "manual"
        trigger_meta: dict[str, Any] = {"trigger": "manual", "waited_seconds": 0.0}
        if mode == "cadence":
            if cadence_trigger == "timer":
                waited_seconds = 0.0
                if cycle_index > 1:
                    waited_seconds = max(0.0, float(interval_seconds))
                    time.sleep(waited_seconds)
                trigger_kind = "timer"
                trigger_meta = {
                    "trigger": "timer",
                    "waited_seconds": round(waited_seconds, 3),
                    "interval_seconds": float(interval_seconds),
                }
            elif cadence_trigger == "timer_or_commit" and cycle_index == 1:
                trigger_kind = "timer"
                trigger_meta = {
                    "trigger": "timer",
                    "waited_seconds": 0.0,
                    "interval_seconds": float(interval_seconds),
                    "git_head": last_git_head,
                    "commits_since_cycle": commits_since_cycle,
                    "total_commit_observations": total_commit_observations,
                    "git_unavailable_polls": git_unavailable_polls,
                }
            else:
                trigger_meta = _wait_for_cadence_trigger(
                    workspace_root=service.workspace_root,
                    cadence_trigger=cadence_trigger,
                    interval_seconds=interval_seconds,
                    git_check_seconds=git_check_seconds,
                    commit_every=commit_every,
                    next_due_monotonic=next_due_monotonic,
                    last_head=last_git_head,
                    commits_since_cycle=commits_since_cycle,
                    total_commit_observations=total_commit_observations,
                    git_unavailable_polls=git_unavailable_polls,
                )
                trigger_kind = str(trigger_meta.get("trigger") or "timer")
                head_value = trigger_meta.get("git_head")
                if isinstance(head_value, str) and head_value:
                    last_git_head = head_value
                commits_since_cycle = int(trigger_meta.get("commits_since_cycle", commits_since_cycle))
                total_commit_observations = int(
                    trigger_meta.get("total_commit_observations", total_commit_observations)
                )
                git_unavailable_polls = int(trigger_meta.get("git_unavailable_polls", git_unavailable_polls))
        trigger_counts[trigger_kind] = trigger_counts.get(trigger_kind, 0) + 1
        cycle_started = _utc_now()
        cycle_payload = _run_cycle(
            service,
            allow_sensitive=allow_sensitive,
            apply=apply,
            max_claims=max_claims,
            max_proposals=max_proposals,
            max_probe_files=max_probe_files,
            max_probe_file_bytes=max_probe_file_bytes,
            max_tool_probes=max_tool_probes,
            probe_timeout_seconds=probe_timeout_seconds,
            probe_failure_threshold=probe_failure_threshold,
            enable_semantic_probe=enable_semantic_probe,
            enable_tool_probe=enable_tool_probe,
        )
        cycle_payload["cycle"] = cycle_index
        cycle_payload["started_at"] = cycle_started
        cycle_payload["finished_at"] = _utc_now()
        cycle_payload["trigger"] = trigger_kind
        cycle_payload["trigger_meta"] = trigger_meta
        decision_summary: dict[str, int] = {}
        for item in cycle_payload.get("decisions", []):
            if not isinstance(item, dict):
                continue
            decision_key = str(item.get("decision") or "unknown")
            decision_summary[decision_key] = decision_summary.get(decision_key, 0) + 1
        cycle_payload["decision_summary"] = dict(sorted(decision_summary.items()))
        cycles.append(cycle_payload)
        if mode == "cadence" and cadence_trigger in {"commit", "timer_or_commit"}:
            commits_since_cycle = 0
            next_due_monotonic = time.monotonic() + max(0.0, float(interval_seconds))

    finished_at = _utc_now()
    duration_ms = round((time.monotonic() - started_monotonic) * 1000.0, 3)
    overall_before = cycles[0]["before"] if cycles else {}
    overall_after = cycles[-1]["after"] if cycles else {}
    report = {
        "schema_version": "steward_report.v2",
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "mode": mode,
        "cadence_trigger": cadence_trigger,
        "apply": apply,
        "workspace_root": str(service.workspace_root),
        "git_check_seconds": git_check_seconds,
        "commit_every": commit_every,
        "probe_timeout_seconds": probe_timeout_seconds,
        "probe_failure_threshold": probe_failure_threshold,
        "enable_semantic_probe": enable_semantic_probe,
        "enable_tool_probe": enable_tool_probe,
        "max_tool_probes": max_tool_probes,
        "cycles_completed": len(cycles),
        "cycles": cycles,
        "before": overall_before,
        "after": overall_after,
        "delta": _status_delta(overall_before, overall_after),
        "run_metadata": {
            "trigger_counts": trigger_counts,
            "initial_git_head": initial_git_head,
            "final_git_head": last_git_head,
            "total_commit_observations": total_commit_observations,
            "git_unavailable_polls": git_unavailable_polls,
            "limits": {
                "max_cycles": max_cycles,
                "max_claims": max_claims,
                "max_proposals": max_proposals,
                "max_probe_files": max_probe_files,
                "max_probe_file_bytes": max_probe_file_bytes,
                "max_tool_probes": max_tool_probes,
            },
            "probe_settings": {
                "probe_timeout_seconds": probe_timeout_seconds,
                "probe_failure_threshold": probe_failure_threshold,
                "enable_semantic_probe": enable_semantic_probe,
                "enable_tool_probe": enable_tool_probe,
            },
            "cadence": {
                "mode": mode,
                "trigger": cadence_trigger if mode == "cadence" else "manual",
                "interval_seconds": interval_seconds,
                "git_check_seconds": git_check_seconds,
                "commit_every": commit_every,
            },
        },
        "artifact_path": str(Path(artifact_path)),
    }
    _write_artifact(Path(artifact_path), report)
    return report
