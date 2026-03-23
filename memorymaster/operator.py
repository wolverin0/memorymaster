from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memorymaster.models import CitationInput
from memorymaster.service import MemoryService
from memorymaster.turn_schema import normalize_turn_row

_SPACE_RE = re.compile(r"\s+")
_PRIVATE_BLOCK_RE = re.compile(r"<\s*private\s*>.*?<\s*/\s*private\s*>", re.IGNORECASE | re.DOTALL)
_EMAIL_VALUE_RE = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
_PATH_VALUE_RE = r"(?:[A-Za-z]:\\[^\s,;]+|/(?:[^/\s]+/)*[^/\s,;]+)"
_TOKEN_RE = re.compile(r"\btoken=([^\s,;]+)", re.IGNORECASE)
_EMAIL_RE = re.compile(
    rf"\b([A-Za-z][A-Za-z0-9 _\-]{{0,64}}?)\s+email\s+is\s+({_EMAIL_VALUE_RE})\b",
    re.IGNORECASE,
)
_DEADLINE_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9 _\-]{0,64}?)\s+deadline\s+is\s+(\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)
_ADDRESS_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9 _\-]{0,64}?)\s+address\s+is\s+([^.;\n]+)",
    re.IGNORECASE,
)
_PATH_RE = re.compile(
    rf"\b([A-Za-z][A-Za-z0-9 _\-]{{0,64}}?)\s+path\s+is\s+({_PATH_VALUE_RE})",
    re.IGNORECASE,
)
_GENERIC_IS_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9 _\-]{1,64})\s+is\s+([^.;\n]{2,120})",
    re.IGNORECASE,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean_chunk(value: str) -> str:
    return value.strip().strip(" \t\r\n,;:.")


def _strip_prefix_words(value: str) -> str:
    lowered = value.strip().lower()
    for prefix in (
        "the ",
        "our ",
        "my ",
        "a ",
        "an ",
        "current ",
        "official ",
        "correction ",
        "corrected ",
        "update ",
        "updated ",
    ):
        if lowered.startswith(prefix):
            return value.strip()[len(prefix) :].strip()
    return value.strip()


def _to_identifier(value: str, fallback: str) -> str:
    clean = _strip_prefix_words(_clean_chunk(value))
    if not clean:
        return fallback
    clean = _SPACE_RE.sub("_", clean.lower())
    clean = re.sub(r"[^a-z0-9_]+", "_", clean).strip("_")
    return clean or fallback


def strip_private_blocks(text: str) -> str:
    if not text:
        return ""
    return _PRIVATE_BLOCK_RE.sub(" ", text)


@dataclass(slots=True)
class OperatorConfig:
    reconcile_interval_seconds: float = 300.0
    policy_mode: str = "legacy"
    policy_limit: int = 200
    retrieval_mode: str = "legacy"
    retrieval_limit: int = 20
    progressive_retrieval: bool = True
    tier1_limit: int = 4
    tier2_limit: int = 8
    min_citations: int = 1
    min_score: float = 0.58
    compact_every: int = 0
    compact_retain_days: int = 30
    compact_event_retain_days: int = 60
    max_idle_seconds: float | None = None
    log_jsonl_path: str | None = "artifacts/operator/operator_events.jsonl"
    state_json_path: str | None = "artifacts/operator/operator_state.json"
    queue_state_json_path: str | None = "artifacts/operator/operator_queue_state.json"
    queue_journal_jsonl_path: str | None = "artifacts/operator/operator_queue_journal.jsonl"
    queue_db_path: str | None = None


@dataclass(slots=True)
class TurnInput:
    session_id: str
    thread_id: str
    turn_id: str
    user_text: str
    assistant_text: str
    observations: list[str]
    timestamp: str


class HeuristicClaimExtractor:
    def extract(self, text: str) -> list[dict[str, object]]:
        raw = text.strip()
        if not raw:
            return []

        claims: list[dict[str, object]] = []
        seen: set[tuple[str, str, str]] = set()

        def add_claim(
            *,
            claim_text: str,
            subject: str,
            predicate: str,
            object_value: str,
            claim_type: str,
            volatility: str,
            confidence: float,
        ) -> None:
            text_value = _clean_chunk(claim_text)
            object_clean = _clean_chunk(object_value)
            if not text_value or not object_clean:
                return
            key = (subject, predicate, object_clean.lower())
            if key in seen:
                return
            seen.add(key)
            claims.append(
                {
                    "text": text_value,
                    "subject": subject,
                    "predicate": predicate,
                    "object_value": object_clean,
                    "claim_type": claim_type,
                    "volatility": volatility,
                    "confidence": max(0.0, min(1.0, float(confidence))),
                }
            )

        for match in _TOKEN_RE.finditer(raw):
            token_value = _clean_chunk(match.group(1))
            if not token_value:
                continue
            add_claim(
                claim_text=f"token={token_value}",
                subject="auth",
                predicate="token",
                object_value=token_value,
                claim_type="security_fact",
                volatility="high",
                confidence=0.96,
            )

        for match in _EMAIL_RE.finditer(raw):
            subject = _to_identifier(match.group(1), fallback="contact")
            email_value = _clean_chunk(match.group(2)).lower()
            add_claim(
                claim_text=match.group(0),
                subject=subject,
                predicate="email",
                object_value=email_value,
                claim_type="contact_fact",
                volatility="medium",
                confidence=0.86,
            )

        for match in _DEADLINE_RE.finditer(raw):
            subject = _to_identifier(match.group(1), fallback="project")
            add_claim(
                claim_text=match.group(0),
                subject=subject,
                predicate="deadline",
                object_value=match.group(2),
                claim_type="schedule_fact",
                volatility="high",
                confidence=0.84,
            )

        for match in _ADDRESS_RE.finditer(raw):
            subject = _to_identifier(match.group(1), fallback="contact")
            add_claim(
                claim_text=match.group(0),
                subject=subject,
                predicate="address",
                object_value=match.group(2),
                claim_type="contact_fact",
                volatility="low",
                confidence=0.80,
            )

        for match in _PATH_RE.finditer(raw):
            subject = _to_identifier(match.group(1), fallback="workspace")
            add_claim(
                claim_text=match.group(0),
                subject=subject,
                predicate="path",
                object_value=match.group(2),
                claim_type="filesystem_fact",
                volatility="medium",
                confidence=0.86,
            )

        for match in _GENERIC_IS_RE.finditer(raw):
            lhs = _clean_chunk(match.group(1))
            rhs = _clean_chunk(match.group(2))
            lhs_lower = lhs.lower()
            if not lhs or not rhs:
                continue
            if any(token in lhs_lower for token in (" email", " deadline", " address", " path")):
                continue
            if "token=" in rhs.lower():
                continue

            words = [w for w in lhs_lower.split() if w]
            if len(words) >= 2:
                subject = _to_identifier(" ".join(words[:-1]), fallback=words[0])
                predicate = _to_identifier(words[-1], fallback="value")
            else:
                subject = _to_identifier(lhs_lower, fallback="entity")
                predicate = "value"
            add_claim(
                claim_text=match.group(0),
                subject=subject,
                predicate=predicate,
                object_value=rhs,
                claim_type="generic_fact",
                volatility="medium",
                confidence=0.62,
            )

        return claims


class MemoryOperator:
    def __init__(
        self,
        service: MemoryService,
        *,
        config: OperatorConfig | None = None,
        extractor: HeuristicClaimExtractor | None = None,
    ) -> None:
        self.service = service
        self.config = config or OperatorConfig()
        self.extractor = extractor or HeuristicClaimExtractor()
        self._reconcile_counter = 0

    def process_turn(self, turn: TurnInput) -> dict[str, object]:
        if self.config.progressive_retrieval:
            tier1 = self.service.query(
                turn.user_text,
                retrieval_mode=self.config.retrieval_mode,
                limit=self.config.tier1_limit,
                include_stale=False,
                include_conflicted=False,
                allow_sensitive=False,
            )
            if tier1:
                retrieved = tier1
                retrieval_meta = {"mode": "progressive", "tier_used": "tier1", "rows": len(retrieved)}
            else:
                retrieved = self.service.query(
                    turn.user_text,
                    retrieval_mode=self.config.retrieval_mode,
                    limit=self.config.tier2_limit,
                    include_stale=True,
                    include_conflicted=True,
                    allow_sensitive=False,
                )
                retrieval_meta = {"mode": "progressive", "tier_used": "tier2", "rows": len(retrieved)}
        else:
            retrieved = self.service.query(
                turn.user_text,
                retrieval_mode=self.config.retrieval_mode,
                limit=self.config.retrieval_limit,
                allow_sensitive=False,
            )
            retrieval_meta = {"mode": "single", "tier_used": "single", "rows": len(retrieved)}

        user_text = strip_private_blocks(turn.user_text).strip()
        assistant_text = strip_private_blocks(turn.assistant_text).strip()
        cleaned_observations = [strip_private_blocks(obs).strip() for obs in turn.observations if obs and obs.strip()]

        parts = [user_text, assistant_text]
        parts.extend(obs for obs in cleaned_observations if obs)
        combined = "\n".join(part for part in parts if part)
        extracted = self.extractor.extract(combined)

        locator_base = f"session={turn.session_id};thread={turn.thread_id};turn={turn.turn_id}"
        evidence: list[tuple[str, str]] = []
        if user_text:
            evidence.append(("user", user_text))
        if assistant_text:
            evidence.append(("assistant", assistant_text))
        for obs in cleaned_observations:
            text = obs.strip()
            if text:
                evidence.append(("observation", text))
        ingested: list[dict[str, object]] = []
        for item in extracted:
            citations: list[CitationInput] = []
            for idx, (kind, text) in enumerate(evidence):
                citations.append(
                    CitationInput(
                        source=f"session://operator/{kind}",
                        locator=f"{locator_base};evidence={idx}",
                        excerpt=text[:500],
                    )
                )
            if not citations:
                citations = [
                    CitationInput(
                        source="session://operator",
                        locator=locator_base,
                        excerpt=str(item["text"]),
                    )
                ]
            claim = self.service.ingest(
                text=str(item["text"]),
                citations=citations,
                claim_type=str(item["claim_type"]),
                subject=str(item["subject"]),
                predicate=str(item["predicate"]),
                object_value=str(item["object_value"]),
                volatility=str(item["volatility"]),
                confidence=float(item["confidence"]),
            )
            ingested.append(
                {
                    "id": claim.id,
                    "status": claim.status,
                    "subject": claim.subject,
                    "predicate": claim.predicate,
                    "object_value": claim.object_value,
                }
            )

        cycle = self.service.run_cycle(
            run_compactor=False,
            min_citations=self.config.min_citations,
            min_score=self.config.min_score,
            policy_mode=self.config.policy_mode,
            policy_limit=self.config.policy_limit,
        )

        return {
            "retrieved": [
                {
                    "id": claim.id,
                    "status": claim.status,
                    "confidence": claim.confidence,
                    "text": claim.text,
                }
                for claim in retrieved
            ],
            "retrieval_meta": retrieval_meta,
            "extracted": extracted,
            "ingested": ingested,
            "cycle": cycle,
        }

    def run_reconcile_once(self) -> dict[str, object]:
        self._reconcile_counter += 1
        cycle = self.service.run_cycle(
            run_compactor=False,
            min_citations=self.config.min_citations,
            min_score=self.config.min_score,
            policy_mode=self.config.policy_mode,
            policy_limit=self.config.policy_limit,
        )

        compacted: dict[str, int] | None = None
        if self.config.compact_every > 0 and self._reconcile_counter % self.config.compact_every == 0:
            compacted = self.service.compact(
                retain_days=self.config.compact_retain_days,
                event_retain_days=self.config.compact_event_retain_days,
            )

        return {
            "reconcile_count": self._reconcile_counter,
            "cycle": cycle,
            "compacted": compacted,
        }

    def run_stream(
        self,
        inbox_jsonl: Path,
        *,
        poll_seconds: float = 1.0,
        max_events: int | None = None,
    ) -> dict[str, object]:
        # Dispatch to SQLite-backed queue when queue_db_path is configured
        if self.config.queue_db_path and str(self.config.queue_db_path).strip():
            return self._run_stream_sqlite(
                inbox_jsonl,
                poll_seconds=poll_seconds,
                max_events=max_events,
            )
        return self._run_stream_json(
            inbox_jsonl,
            poll_seconds=poll_seconds,
            max_events=max_events,
        )

    def _parse_pending_queue(self, raw_pending, next_queue_id: int) -> tuple[list[dict[str, Any]], int]:
        """Parse pending queue entries from raw state."""
        pending_queue: list[dict[str, Any]] = []
        max_entry_id = 0

        if not isinstance(raw_pending, list):
            raise ValueError("queue_state pending field is not a list")

        for idx, raw_entry in enumerate(raw_pending):
            if not isinstance(raw_entry, dict):
                raise ValueError("queue_state pending entry is not an object")
            payload = str(raw_entry.get("payload", "")).strip().lstrip("\ufeff")
            if not payload:
                continue
            entry_offset = max(0, int(raw_entry.get("offset", 0)))
            entry_id_raw = raw_entry.get("entry_id")
            entry_id = next_queue_id + idx if entry_id_raw is None else max(1, int(entry_id_raw))
            max_entry_id = max(max_entry_id, entry_id)
            pending_queue.append({
                "entry_id": entry_id,
                "offset": entry_offset,
                "payload": payload,
            })

        final_queue_id = max(next_queue_id, max_entry_id + 1) if max_entry_id >= next_queue_id else next_queue_id
        return pending_queue, final_queue_id

    def _load_legacy_state(self, state_path: Path | None, canonical_inbox: str, emit) -> dict[str, int] | None:
        """Load legacy state from file."""
        if state_path is None or not state_path.exists():
            return None

        try:
            raw_state = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(raw_state, dict):
                raise ValueError("state_json is not an object")
            state_inbox_raw = str(raw_state.get("inbox_jsonl", "")).strip()
            state_inbox = str(Path(state_inbox_raw).resolve()) if state_inbox_raw else ""
            if state_inbox and state_inbox == canonical_inbox:
                legacy_state = {
                    "offset": max(0, int(raw_state.get("offset", 0))),
                    "seen_events": max(0, int(raw_state.get("seen_events", 0))),
                    "processed_events": max(0, int(raw_state.get("processed_events", 0))),
                }
                emit(
                    "state_loaded",
                    {
                        "state_json": str(state_path),
                        "inbox_jsonl": canonical_inbox,
                        "offset": legacy_state["offset"],
                        "seen_events": legacy_state["seen_events"],
                        "processed_events": legacy_state["processed_events"],
                    },
                )
                return legacy_state
        except Exception as exc:
            emit(
                "state_error",
                {
                    "op": "load",
                    "state_json": str(state_path),
                    "error": str(exc),
                },
            )
        return None

    def _load_queue_state(self, queue_state_path: Path | None, canonical_inbox: str, emit) -> tuple[int, int, int, int, int, list[dict[str, Any]], bool]:
        """Load queue state from file. Returns (start_offset, read_offset, acked_offset, seen_events, processed_events, pending_queue, loaded)."""
        start_offset = 0
        read_offset = 0
        acked_offset = 0
        persisted_seen_events = 0
        persisted_processed_events = 0
        next_queue_id = 1
        pending_queue: list[dict[str, Any]] = []
        queue_state_loaded = False

        if queue_state_path is None or not queue_state_path.exists():
            return start_offset, read_offset, acked_offset, persisted_seen_events, persisted_processed_events, pending_queue, queue_state_loaded

        try:
            raw_queue_state = json.loads(queue_state_path.read_text(encoding="utf-8"))
            if not isinstance(raw_queue_state, dict):
                raise ValueError("queue_state_json is not an object")
            queue_inbox_raw = str(raw_queue_state.get("inbox_jsonl", "")).strip()
            queue_inbox = str(Path(queue_inbox_raw).resolve()) if queue_inbox_raw else ""
            if not (queue_inbox and queue_inbox == canonical_inbox):
                return start_offset, read_offset, acked_offset, persisted_seen_events, persisted_processed_events, pending_queue, queue_state_loaded

            start_offset = max(0, int(raw_queue_state.get("read_offset", raw_queue_state.get("offset", 0))))
            read_offset = start_offset
            acked_offset = max(0, int(raw_queue_state.get("acked_offset", raw_queue_state.get("offset", read_offset))))
            persisted_seen_events = max(0, int(raw_queue_state.get("seen_events", 0)))
            persisted_processed_events = max(0, int(raw_queue_state.get("processed_events", 0)))
            next_queue_id = max(1, int(raw_queue_state.get("next_queue_id", 1)))

            raw_pending = raw_queue_state.get("pending", [])
            pending_queue, next_queue_id = self._parse_pending_queue(raw_pending, next_queue_id)

            queue_state_loaded = True
            emit(
                "queue_state_loaded",
                {
                    "queue_state_json": str(queue_state_path),
                    "inbox_jsonl": canonical_inbox,
                    "read_offset": read_offset,
                    "acked_offset": acked_offset,
                    "pending_events": len(pending_queue),
                    "seen_events": persisted_seen_events,
                    "processed_events": persisted_processed_events,
                },
            )
        except Exception as exc:
            emit(
                "state_error",
                {
                    "op": "load_queue_state",
                    "queue_state_json": str(queue_state_path),
                    "error": str(exc),
                },
            )
            queue_state_loaded = False

        return start_offset, read_offset, acked_offset, persisted_seen_events, persisted_processed_events, pending_queue, queue_state_loaded

    def _initialize_queue_state_from_legacy(
        self,
        queue_state_loaded: bool,
        legacy_state: dict[str, int] | None,
        start_offset: int,
        read_offset: int,
        acked_offset: int,
        persisted_seen_events: int,
        persisted_processed_events: int,
        state_path: Path | None,
        canonical_inbox: str,
        emit,
    ) -> tuple[int, int, int, int, int]:
        """Initialize queue state from legacy state if needed. Returns (start_offset, read_offset, acked_offset, seen_events, processed_events)."""
        if not queue_state_loaded and legacy_state is not None:
            start_offset = legacy_state["offset"]
            read_offset = start_offset
            acked_offset = start_offset
            persisted_seen_events = legacy_state["seen_events"]
            persisted_processed_events = legacy_state["processed_events"]
            emit(
                "queue_state_bootstrap_legacy",
                {
                    "state_json": str(state_path) if state_path is not None else None,
                    "inbox_jsonl": canonical_inbox,
                    "offset": start_offset,
                },
            )
        elif not queue_state_loaded and legacy_state is None:
            read_offset = start_offset
            acked_offset = start_offset

        return start_offset, read_offset, acked_offset, persisted_seen_events, persisted_processed_events

    def _seek_to_offset(
        self,
        handle,
        inbox: Path,
        read_offset: int,
        state_path: Path | None,
        queue_state_path: Path | None,
        emit,
    ) -> tuple[int, int]:
        """Seek file handle to read_offset, handling errors. Returns (start_offset, read_offset)."""
        start_offset = 0
        if read_offset > 0:
            try:
                file_size = inbox.stat().st_size
                if read_offset > file_size:
                    raise ValueError(f"offset {read_offset} exceeds file size {file_size}")
                handle.seek(read_offset)
            except Exception as exc:
                emit(
                    "state_error",
                    {
                        "op": "seek",
                        "state_json": str(state_path) if state_path is not None else None,
                        "queue_state_json": str(queue_state_path) if queue_state_path is not None else None,
                        "error": str(exc),
                    },
                )
                start_offset = 0
                read_offset = 0
                handle.seek(0)
        return start_offset, read_offset

    def _handle_queue_json_error(self, entry_id: int, entry_offset: int, exc: Exception, emit, append_queue_journal) -> None:
        """Handle JSON decode error for a queue entry."""
        emit(
            "json_error",
            {
                "entry_id": entry_id,
                "offset": entry_offset,
                "error_kind": "json_decode",
                "error": str(exc),
            },
        )
        append_queue_journal(
            "failed",
            {
                "entry_id": entry_id,
                "offset": entry_offset,
                "error_kind": "json_decode",
                "error": str(exc),
            },
        )

    def _handle_queue_process_error(self, entry_id: int, entry_offset: int, exc: Exception, emit, append_queue_journal) -> None:
        """Handle process turn error for a queue entry."""
        emit(
            "queue_entry_error",
            {
                "entry_id": entry_id,
                "offset": entry_offset,
                "error_kind": "process_turn",
                "error": str(exc),
            },
        )
        append_queue_journal(
            "failed",
            {
                "entry_id": entry_id,
                "offset": entry_offset,
                "error_kind": "process_turn",
                "error": str(exc),
            },
        )

    def _record_turn_processed(
        self,
        turn,
        summary: dict[str, object],
        entry_id: int,
        entry_offset: int,
        seen_events: int,
        processed_events: int,
        processed_turns: list[dict[str, Any]],
        pending_queue: list[dict[str, Any]],
        emit,
        append_queue_journal,
    ) -> None:
        """Record processed turn in stats and emit/journal events."""
        processed_turns.append(
            {
                "turn_id": turn.turn_id,
                "retrieval_mode": str(summary.get("retrieval_meta", {}).get("mode", "")),
                "retrieval_tier": str(summary.get("retrieval_meta", {}).get("tier_used", "")),
                "retrieval_rows": int(summary.get("retrieval_meta", {}).get("rows", 0)),
                "extracted": len(summary["extracted"]),
                "ingested": len(summary["ingested"]),
            }
        )
        emit(
            "turn_processed",
            {
                "turn_id": turn.turn_id,
                "entry_id": entry_id,
                "seen_events": seen_events,
                "processed_events": processed_events,
                "retrieval_mode": str(summary.get("retrieval_meta", {}).get("mode", "")),
                "retrieval_tier": str(summary.get("retrieval_meta", {}).get("tier_used", "")),
                "retrieval_rows": int(summary.get("retrieval_meta", {}).get("rows", 0)),
                "extracted": len(summary["extracted"]),
                "ingested": len(summary["ingested"]),
                "pending_events": len(pending_queue),
            },
        )
        append_queue_journal(
            "ack",
            {
                "entry_id": entry_id,
                "offset": entry_offset,
                "turn_id": turn.turn_id,
                "pending_events": len(pending_queue),
            },
        )

    def _enqueue_new_event(
        self,
        payload: str,
        next_queue_id: int,
        current_offset: int,
        seen_events: int,
        pending_queue: list[dict[str, Any]],
        emit,
        append_queue_journal,
    ) -> int:
        """Enqueue a new event. Returns updated next_queue_id."""
        entry_id = next_queue_id
        next_queue_id += 1
        pending_queue.append(
            {
                "entry_id": entry_id,
                "offset": current_offset,
                "payload": payload,
            }
        )
        emit(
            "queue_enqueued",
            {
                "entry_id": entry_id,
                "offset": current_offset,
                "seen_events": seen_events,
                "pending_events": len(pending_queue),
            },
        )
        append_queue_journal(
            "enqueue",
            {
                "entry_id": entry_id,
                "offset": current_offset,
                "pending_events": len(pending_queue),
            },
        )
        return next_queue_id

    def _run_stream_json(
        self,
        inbox_jsonl: Path,
        *,
        poll_seconds: float = 1.0,
        max_events: int | None = None,
    ) -> dict[str, object]:
        """Legacy JSON-file backed stream processing (original implementation)."""
        inbox = Path(inbox_jsonl)
        inbox.parent.mkdir(parents=True, exist_ok=True)
        inbox.touch(exist_ok=True)
        log_path = None
        if self.config.log_jsonl_path:
            log_path = Path(self.config.log_jsonl_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
        state_path = None
        if self.config.state_json_path and str(self.config.state_json_path).strip():
            state_path = Path(str(self.config.state_json_path).strip())
        queue_state_path = None
        if self.config.queue_state_json_path and str(self.config.queue_state_json_path).strip():
            queue_state_path = Path(str(self.config.queue_state_json_path).strip())
        queue_journal_path = None
        if self.config.queue_journal_jsonl_path and str(self.config.queue_journal_jsonl_path).strip():
            queue_journal_path = Path(str(self.config.queue_journal_jsonl_path).strip())
            queue_journal_path.parent.mkdir(parents=True, exist_ok=True)

        def emit(event: str, payload: dict[str, object] | None = None) -> None:
            if log_path is None:
                return
            record = {"ts": _utc_now_iso(), "event": event}
            if payload:
                record.update(payload)
            with log_path.open("a", encoding="utf-8") as out:
                out.write(json.dumps(record, ensure_ascii=True) + "\n")

        canonical_inbox = str(inbox.resolve())
        start_offset = 0
        read_offset = 0
        acked_offset = 0
        persisted_seen_events = 0
        persisted_processed_events = 0
        pending_queue: list[dict[str, Any]] = []
        next_queue_id = 1

        def append_queue_journal(event: str, payload: dict[str, object] | None = None) -> None:
            if queue_journal_path is None:
                return
            record: dict[str, object] = {"ts": _utc_now_iso(), "event": event, "inbox_jsonl": canonical_inbox}
            if payload:
                record.update(payload)
            try:
                with queue_journal_path.open("a", encoding="utf-8") as out:
                    out.write(json.dumps(record, ensure_ascii=True) + "\n")
            except Exception as exc:
                emit(
                    "state_error",
                    {
                        "op": "append_queue_journal",
                        "queue_journal_jsonl": str(queue_journal_path),
                        "error": str(exc),
                    },
                )

        legacy_state = self._load_legacy_state(state_path, canonical_inbox, emit)

        start_offset, read_offset, acked_offset, persisted_seen_events, persisted_processed_events, pending_queue, queue_state_loaded = (
            self._load_queue_state(queue_state_path, canonical_inbox, emit)
        )
        next_queue_id = max(1, max((e.get("entry_id", 0) for e in pending_queue), default=0) + 1) if pending_queue else 1

        start_offset, read_offset, acked_offset, persisted_seen_events, persisted_processed_events = (
            self._initialize_queue_state_from_legacy(
                queue_state_loaded, legacy_state,
                start_offset, read_offset, acked_offset, persisted_seen_events, persisted_processed_events,
                state_path, canonical_inbox, emit
            )
        )

        def persist_queue_state() -> None:
            if queue_state_path is None:
                return
            queue_state_payload = {
                "inbox_jsonl": canonical_inbox,
                "read_offset": max(0, int(read_offset)),
                "acked_offset": max(0, int(acked_offset)),
                "seen_events": persisted_seen_events,
                "processed_events": persisted_processed_events,
                "next_queue_id": max(1, int(next_queue_id)),
                "pending": [
                    {
                        "entry_id": max(1, int(entry["entry_id"])),
                        "offset": max(0, int(entry["offset"])),
                        "payload": str(entry["payload"]),
                    }
                    for entry in pending_queue
                ],
                "updated_at": _utc_now_iso(),
            }
            try:
                queue_state_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = queue_state_path.with_suffix(f"{queue_state_path.suffix}.tmp")
                tmp_path.write_text(json.dumps(queue_state_payload, ensure_ascii=True) + "\n", encoding="utf-8")
                tmp_path.replace(queue_state_path)
                emit(
                    "queue_state_saved",
                    {
                        "queue_state_json": str(queue_state_path),
                        "inbox_jsonl": canonical_inbox,
                        "read_offset": queue_state_payload["read_offset"],
                        "acked_offset": queue_state_payload["acked_offset"],
                        "pending_events": len(queue_state_payload["pending"]),
                        "seen_events": queue_state_payload["seen_events"],
                        "processed_events": queue_state_payload["processed_events"],
                    },
                )
            except Exception as exc:
                emit(
                    "state_error",
                    {
                        "op": "save_queue_state",
                        "queue_state_json": str(queue_state_path),
                        "error": str(exc),
                    },
                )

        def persist_state() -> None:
            if state_path is None:
                return
            state_payload = {
                "inbox_jsonl": canonical_inbox,
                "offset": max(0, int(acked_offset)),
                "read_offset": max(0, int(read_offset)),
                "pending_events": len(pending_queue),
                "seen_events": persisted_seen_events,
                "processed_events": persisted_processed_events,
                "updated_at": _utc_now_iso(),
            }
            try:
                state_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = state_path.with_suffix(f"{state_path.suffix}.tmp")
                tmp_path.write_text(json.dumps(state_payload, ensure_ascii=True) + "\n", encoding="utf-8")
                tmp_path.replace(state_path)
                emit(
                    "state_saved",
                    {
                        "state_json": str(state_path),
                        "inbox_jsonl": canonical_inbox,
                        "offset": state_payload["offset"],
                        "read_offset": state_payload["read_offset"],
                        "pending_events": state_payload["pending_events"],
                        "seen_events": state_payload["seen_events"],
                        "processed_events": state_payload["processed_events"],
                    },
                )
            except Exception as exc:
                emit(
                    "state_error",
                    {
                        "op": "save",
                        "state_json": str(state_path),
                        "error": str(exc),
                    },
                )

        def persist_durability_state() -> None:
            persist_queue_state()
            persist_state()

        processed_events = 0
        seen_events = 0
        processed_turns: list[dict[str, Any]] = []
        json_errors = 0
        reconciles = 0
        started = time.monotonic()
        last_reconcile = started
        last_activity = started
        exit_reason = "stream_stopped"
        final_offset = read_offset
        emit(
            "stream_start",
            {
                "inbox_jsonl": str(inbox),
                "max_events": max_events,
                "poll_seconds": poll_seconds,
                "max_idle_seconds": self.config.max_idle_seconds,
                "start_offset": start_offset,
                "pending_events": len(pending_queue),
                "queue_state_json": str(queue_state_path) if queue_state_path is not None else None,
                "queue_journal_jsonl": str(queue_journal_path) if queue_journal_path is not None else None,
            },
        )

        with inbox.open("rb") as handle:
            start_offset, read_offset = self._seek_to_offset(
                handle, inbox, read_offset, state_path, queue_state_path, emit
            )
            final_offset = handle.tell()
            while True:
                queue_blocked = False
                while pending_queue:
                    head = pending_queue[0]
                    entry_id = max(1, int(head["entry_id"]))
                    entry_offset = max(0, int(head["offset"]))
                    payload = str(head["payload"])
                    try:
                        row = json.loads(payload)
                    except json.JSONDecodeError as exc:
                        json_errors += 1
                        self._handle_queue_json_error(entry_id, entry_offset, exc, emit, append_queue_journal)
                        pending_queue.pop(0)
                        continue

                    try:
                        turn = self._turn_from_row(row)
                        summary = self.process_turn(turn)
                    except Exception as exc:
                        exit_reason = "queue_entry_error"
                        self._handle_queue_process_error(entry_id, entry_offset, exc, emit, append_queue_journal)
                        queue_blocked = True
                        break

                    pending_queue.pop(0)
                    acked_offset = max(acked_offset, entry_offset)
                    processed_events += 1
                    persisted_processed_events += 1
                    self._record_turn_processed(
                        turn, summary, entry_id, entry_offset,
                        seen_events, processed_events, processed_turns,
                        pending_queue, emit, append_queue_journal
                    )
                    persist_durability_state()
                    last_activity = time.monotonic()

                if queue_blocked:
                    break

                if max_events is not None and seen_events >= max_events:
                    exit_reason = "max_events_reached"
                    break

                line = handle.readline()
                current_offset = handle.tell()
                if line:
                    last_activity = time.monotonic()
                    final_offset = current_offset
                    payload = line.decode("utf-8", errors="replace").strip().lstrip("\ufeff")
                    read_offset = current_offset
                    if not payload:
                        persist_durability_state()
                        continue

                    seen_events += 1
                    persisted_seen_events += 1
                    next_queue_id = self._enqueue_new_event(
                        payload, next_queue_id, current_offset,
                        seen_events, pending_queue, emit, append_queue_journal
                    )
                    persist_durability_state()
                    continue

                final_offset = current_offset
                if self.config.reconcile_interval_seconds > 0:
                    now = time.monotonic()
                    if (now - last_reconcile) >= self.config.reconcile_interval_seconds:
                        reconcile_result = self.run_reconcile_once()
                        reconciles += 1
                        last_reconcile = now
                        emit(
                            "reconcile_run",
                            {
                                "reconciles": reconciles,
                                "compacted": reconcile_result.get("compacted") is not None,
                            },
                        )

                if (
                    self.config.max_idle_seconds is not None
                    and self.config.max_idle_seconds > 0
                    and (time.monotonic() - last_activity) >= self.config.max_idle_seconds
                ):
                    exit_reason = "idle_timeout"
                    break
                time.sleep(max(0.05, float(poll_seconds)))

        summary = {
            "processed_events": processed_events,
            "seen_events": seen_events,
            "reconciles": reconciles,
            "json_errors": json_errors,
            "exit_reason": exit_reason,
            "start_offset": start_offset,
            "final_offset": final_offset,
            "read_offset": read_offset,
            "acked_offset": acked_offset,
            "pending_events": len(pending_queue),
            "runtime_seconds": round(time.monotonic() - started, 3),
            "turns": processed_turns,
        }
        if log_path is not None:
            summary["log_jsonl"] = str(log_path)
        if queue_state_path is not None:
            summary["queue_state_json"] = str(queue_state_path)
        if queue_journal_path is not None:
            summary["queue_journal_jsonl"] = str(queue_journal_path)
        emit(
            "stream_exit",
            {
                "processed_events": processed_events,
                "seen_events": seen_events,
                "json_errors": json_errors,
                "reconciles": reconciles,
                "exit_reason": exit_reason,
                "pending_events": len(pending_queue),
                "acked_offset": acked_offset,
                "read_offset": read_offset,
            },
        )
        return summary

    def _run_stream_sqlite(
        self,
        inbox_jsonl: Path,
        *,
        poll_seconds: float = 1.0,
        max_events: int | None = None,
    ) -> dict[str, object]:
        """SQLite WAL-backed stream processing -- crash-safe queue."""
        from memorymaster.operator_queue import OperatorQueue

        inbox = Path(inbox_jsonl)
        inbox.parent.mkdir(parents=True, exist_ok=True)
        inbox.touch(exist_ok=True)

        log_path = None
        if self.config.log_jsonl_path:
            log_path = Path(self.config.log_jsonl_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)

        def emit(event: str, payload: dict[str, object] | None = None) -> None:
            if log_path is None:
                return
            record = {"ts": _utc_now_iso(), "event": event}
            if payload:
                record.update(payload)
            with log_path.open("a", encoding="utf-8") as out:
                out.write(json.dumps(record, ensure_ascii=True) + "\n")

        canonical_inbox = str(inbox.resolve())

        # Open / create the SQLite queue
        queue_db_path = Path(str(self.config.queue_db_path).strip())
        queue = OperatorQueue(queue_db_path)

        # Migrate from JSON files on first run
        state_path = None
        if self.config.state_json_path and str(self.config.state_json_path).strip():
            state_path = Path(str(self.config.state_json_path).strip())
        queue_state_path = None
        if self.config.queue_state_json_path and str(self.config.queue_state_json_path).strip():
            queue_state_path = Path(str(self.config.queue_state_json_path).strip())

        migrated = queue.migrate_from_json(queue_state_path, state_path, canonical_inbox)
        if migrated:
            emit("queue_migrated_to_sqlite", {"queue_db": str(queue_db_path)})

        # Store inbox path in meta for validation on next run
        stored_inbox = queue.get_meta("inbox_jsonl")
        if stored_inbox and stored_inbox != canonical_inbox:
            # Different inbox -- reset state
            queue.set_meta("inbox_jsonl", canonical_inbox)
            queue.set_meta_int("read_offset", 0)
            queue.set_meta_int("acked_offset", 0)
            queue.set_meta_int("seen_events", 0)
            queue.set_meta_int("processed_events", 0)
        elif not stored_inbox:
            queue.set_meta("inbox_jsonl", canonical_inbox)

        # Recover any entries that were mid-processing when we crashed
        requeued = queue.requeue_processing()
        if requeued > 0:
            emit("queue_requeued_processing", {"count": requeued})

        read_offset = queue.get_meta_int("read_offset", 0)
        acked_offset = queue.get_meta_int("acked_offset", 0)
        persisted_seen_events = queue.get_meta_int("seen_events", 0)
        persisted_processed_events = queue.get_meta_int("processed_events", 0)
        start_offset = read_offset

        processed_events = 0
        seen_events = 0
        processed_turns: list[dict[str, Any]] = []
        json_errors = 0
        reconciles = 0
        started = time.monotonic()
        last_reconcile = started
        last_activity = started
        exit_reason = "stream_stopped"
        final_offset = read_offset

        emit(
            "stream_start",
            {
                "inbox_jsonl": str(inbox),
                "max_events": max_events,
                "poll_seconds": poll_seconds,
                "max_idle_seconds": self.config.max_idle_seconds,
                "start_offset": start_offset,
                "pending_events": queue.pending_count(),
                "queue_db": str(queue_db_path),
            },
        )

        def persist_meta() -> None:
            queue.set_meta_int("read_offset", max(0, int(read_offset)))
            queue.set_meta_int("acked_offset", max(0, int(acked_offset)))
            queue.set_meta_int("seen_events", persisted_seen_events)
            queue.set_meta_int("processed_events", persisted_processed_events)
            # Also persist legacy state_json for backward compatibility
            if state_path is not None:
                state_payload = {
                    "inbox_jsonl": canonical_inbox,
                    "offset": max(0, int(acked_offset)),
                    "read_offset": max(0, int(read_offset)),
                    "pending_events": queue.pending_count(),
                    "seen_events": persisted_seen_events,
                    "processed_events": persisted_processed_events,
                    "updated_at": _utc_now_iso(),
                }
                try:
                    state_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp_path = state_path.with_suffix(f"{state_path.suffix}.tmp")
                    tmp_path.write_text(
                        json.dumps(state_payload, ensure_ascii=True) + "\n",
                        encoding="utf-8",
                    )
                    tmp_path.replace(state_path)
                    emit(
                        "state_saved",
                        {
                            "state_json": str(state_path),
                            "inbox_jsonl": canonical_inbox,
                            "offset": state_payload["offset"],
                            "read_offset": state_payload["read_offset"],
                            "pending_events": state_payload["pending_events"],
                            "seen_events": state_payload["seen_events"],
                            "processed_events": state_payload["processed_events"],
                        },
                    )
                except Exception as exc:
                    emit(
                        "state_error",
                        {
                            "op": "save",
                            "state_json": str(state_path),
                            "error": str(exc),
                        },
                    )

        try:
            with inbox.open("rb") as handle:
                if read_offset > 0:
                    try:
                        file_size = inbox.stat().st_size
                        if read_offset > file_size:
                            raise ValueError(f"offset {read_offset} exceeds file size {file_size}")
                        handle.seek(read_offset)
                    except Exception as exc:
                        emit(
                            "state_error",
                            {
                                "op": "seek",
                                "queue_db": str(queue_db_path),
                                "error": str(exc),
                            },
                        )
                        start_offset = 0
                        read_offset = 0
                        handle.seek(0)

                final_offset = handle.tell()

                while True:
                    # --- drain pending entries from SQLite queue ---
                    queue_blocked = False
                    while True:
                        entry = queue.dequeue()
                        if entry is None:
                            break

                        try:
                            row = json.loads(entry.payload)
                        except json.JSONDecodeError as exc:
                            json_errors += 1
                            emit(
                                "json_error",
                                {
                                    "entry_id": entry.id,
                                    "offset": entry.inbox_offset,
                                    "error_kind": "json_decode",
                                    "error": str(exc),
                                    "pending_events": queue.pending_count(),
                                },
                            )
                            queue.fail(entry.id, f"json_decode: {exc}")
                            continue

                        try:
                            turn = self._turn_from_row(row)
                            summary = self.process_turn(turn)
                        except Exception as exc:
                            exit_reason = "queue_entry_error"
                            emit(
                                "queue_entry_error",
                                {
                                    "entry_id": entry.id,
                                    "offset": entry.inbox_offset,
                                    "error_kind": "process_turn",
                                    "error": str(exc),
                                    "pending_events": queue.pending_count(),
                                },
                            )
                            queue.fail(entry.id, f"process_turn: {exc}")
                            queue_blocked = True
                            break

                        queue.ack(entry.id)
                        acked_offset = max(acked_offset, entry.inbox_offset)
                        processed_events += 1
                        persisted_processed_events += 1
                        processed_turns.append(
                            {
                                "turn_id": turn.turn_id,
                                "retrieval_mode": str(summary.get("retrieval_meta", {}).get("mode", "")),
                                "retrieval_tier": str(summary.get("retrieval_meta", {}).get("tier_used", "")),
                                "retrieval_rows": int(summary.get("retrieval_meta", {}).get("rows", 0)),
                                "extracted": len(summary["extracted"]),
                                "ingested": len(summary["ingested"]),
                            }
                        )
                        emit(
                            "turn_processed",
                            {
                                "turn_id": turn.turn_id,
                                "entry_id": entry.id,
                                "seen_events": seen_events,
                                "processed_events": processed_events,
                                "retrieval_mode": str(summary.get("retrieval_meta", {}).get("mode", "")),
                                "retrieval_tier": str(summary.get("retrieval_meta", {}).get("tier_used", "")),
                                "retrieval_rows": int(summary.get("retrieval_meta", {}).get("rows", 0)),
                                "extracted": len(summary["extracted"]),
                                "ingested": len(summary["ingested"]),
                                "pending_events": queue.pending_count(),
                            },
                        )
                        persist_meta()
                        last_activity = time.monotonic()

                    if queue_blocked:
                        break

                    if max_events is not None and seen_events >= max_events:
                        exit_reason = "max_events_reached"
                        break

                    # --- read next line from inbox ---
                    line = handle.readline()
                    current_offset = handle.tell()
                    if line:
                        last_activity = time.monotonic()
                        final_offset = current_offset
                        payload = line.decode("utf-8", errors="replace").strip().lstrip("\ufeff")
                        read_offset = current_offset
                        if not payload:
                            persist_meta()
                            continue

                        entry_id = queue.enqueue(payload, inbox_offset=current_offset)
                        seen_events += 1
                        persisted_seen_events += 1
                        emit(
                            "queue_enqueued",
                            {
                                "entry_id": entry_id,
                                "offset": current_offset,
                                "seen_events": seen_events,
                                "pending_events": queue.pending_count(),
                            },
                        )
                        persist_meta()
                        continue

                    final_offset = current_offset
                    if self.config.reconcile_interval_seconds > 0:
                        now = time.monotonic()
                        if (now - last_reconcile) >= self.config.reconcile_interval_seconds:
                            reconcile_result = self.run_reconcile_once()
                            reconciles += 1
                            last_reconcile = now
                            emit(
                                "reconcile_run",
                                {
                                    "reconciles": reconciles,
                                    "compacted": reconcile_result.get("compacted") is not None,
                                },
                            )

                    if (
                        self.config.max_idle_seconds is not None
                        and self.config.max_idle_seconds > 0
                        and (time.monotonic() - last_activity) >= self.config.max_idle_seconds
                    ):
                        exit_reason = "idle_timeout"
                        break
                    time.sleep(max(0.05, float(poll_seconds)))
        finally:
            persist_meta()
            queue.close()

        result: dict[str, object] = {
            "processed_events": processed_events,
            "seen_events": seen_events,
            "reconciles": reconciles,
            "json_errors": json_errors,
            "exit_reason": exit_reason,
            "start_offset": start_offset,
            "final_offset": final_offset,
            "read_offset": read_offset,
            "acked_offset": acked_offset,
            "pending_events": 0,  # queue is closed; count before close is in meta
            "runtime_seconds": round(time.monotonic() - started, 3),
            "turns": processed_turns,
        }
        if log_path is not None:
            result["log_jsonl"] = str(log_path)
        result["queue_db"] = str(queue_db_path)
        emit(
            "stream_exit",
            {
                "processed_events": processed_events,
                "seen_events": seen_events,
                "json_errors": json_errors,
                "reconciles": reconciles,
                "exit_reason": exit_reason,
                "pending_events": 0,
                "acked_offset": acked_offset,
                "read_offset": read_offset,
            },
        )
        return result

    @staticmethod
    def _turn_from_row(row: dict[str, Any]) -> TurnInput:
        normalized = normalize_turn_row(row)
        return TurnInput(
            session_id=normalized.session_id,
            thread_id=normalized.thread_id,
            turn_id=normalized.turn_id,
            user_text=normalized.user_text,
            assistant_text=normalized.assistant_text,
            observations=normalized.observations,
            timestamp=normalized.timestamp or _utc_now_iso(),
        )
