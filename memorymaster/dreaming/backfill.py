"""Operator-started AO-7 backfill, isolated from live capture and activation.

Dry runs never construct a ledger, service or provider. All durable campaign
state lives in the three explicitly named auxiliary artifacts.
"""
from __future__ import annotations

import json
import hashlib
import random
import sqlite3
from contextlib import contextmanager
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from memorymaster.dreaming.backfill_guard import PauseLatch, evaluate_backfill_guard
from memorymaster.dreaming.providers import ProviderCallError
from memorymaster.dreaming.backfill_quality import evaluate_cohort_quality, fingerprint, receipt_is_current
from memorymaster.dreaming.backfill_selection import (
    complete_records, frozen_identity, iter_source_turns, order_inventory,
    select_windows, valid_timestamp, verify_frozen,
)
from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.models import CaptureEnvelope, DreamCandidate, DreamDecision, DreamMessage
from memorymaster.stores._storage_shared import ConcurrentModificationError, connect_ro

LEDGER_NAME = "dream_backfill_ledger.db"
GUARD_NAME = "dream_backfill_guard.json"
STATUS_NAME = "dream_backfill_status.json"


def _require_sqlite_service(service) -> None:
    from memorymaster.stores.storage import SQLiteStore
    from memorymaster.stores.postgres_store import PostgresStore

    if not isinstance(service.store, SQLiteStore) or isinstance(service.store, PostgresStore):
        raise ValueError("backfill source review and conditional retirement are SQLite-only")


@dataclass(frozen=True)
class BackfillConfig:
    state_dir: Path
    roots: Mapping[str, Path] = field(default_factory=dict)
    campaign_id: str = "ao7"
    pilot_sessions: int = 250
    cohort_sessions: int = 1000
    max_extract_daily: int = 1200
    max_consolidate_daily: int = 200
    max_consolidate_cohort: int = 400
    max_apply_daily: int = 200
    extractor_model: str = ""
    consolidator_model: str = ""
    rubric_version: str = "ao7-m5-v2-5pct-90pct"

    def __post_init__(self):
        object.__setattr__(self, "state_dir", Path(self.state_dir))
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.campaign_id):
            raise ValueError("invalid campaign identity")
        if not 1 <= self.pilot_sessions <= 250 or not 1 <= self.cohort_sessions <= 1000:
            raise ValueError("invalid cohort size")
        if any(name not in {"claude", "codex", "agy"} for name in self.roots):
            raise ValueError("unsupported source family")
        if not (1 <= self.max_extract_daily <= 1200 and 1 <= self.max_consolidate_daily <= 200
                and 1 <= self.max_consolidate_cohort <= 400 and 1 <= self.max_apply_daily <= 200):
            raise ValueError("budget exceeds approved envelope")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Temporary sibling is replaced immediately; it never becomes a fourth artifact.
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _header(path: Path, provider: str) -> tuple[str, str | None, str | None, bool]:
    session, cwd, stamp, generated = path.stem, None, None, False
    for index, (_, end, raw) in enumerate(complete_records(path)):
        if index >= 400 or end > 1_048_576:
            break
        if raw is None:
            continue
        try:
            entry = json.loads(raw)
            if not isinstance(entry, dict):
                continue
        except (ValueError, UnicodeError):
            continue
        generated |= bool(entry.get("backfill_generated") or entry.get("memorymaster_generated"))
        stamp = stamp or valid_timestamp(entry.get("timestamp") or entry.get("created_at"))
        if provider == "codex" and entry.get("type") == "session_meta":
            payload = entry.get("payload") or {}
            session = str(payload.get("id") or session)
            cwd = payload.get("cwd") or cwd
            stamp = stamp or valid_timestamp(payload.get("timestamp"))
        elif provider == "claude":
            session = str(entry.get("sessionId") or session)
            cwd = entry.get("cwd") or cwd
    return session, cwd, stamp, generated


@contextmanager
def _read_ledger(path: Path, *, dry_run: bool = False):
    """Offline previews must not create SQLite WAL/SHM sidecars.

    Immutable reads are safe only for a quiescent, checkpointed ledger. Active
    WAL work is explicitly unavailable to an offline preview, never omitted.
    Mutating rollback always reopens the current ledger through ordinary RO.
    """
    wal = Path(str(path) + "-wal")
    if dry_run and wal.exists() and wal.stat().st_size:
        raise ValueError("dry-run requires a checkpointed backfill ledger; active WAL work is not an offline snapshot")
    conn = (sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
            if dry_run else connect_ro(path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


def read_backfill_status(state_dir: Path, *, campaign_id: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Read-only reporting: no schema initialization, migrations or live lag inputs."""
    path = state_dir / LEDGER_NAME
    if not path.exists():
        return {"state": "not_inventoried", "sessions": 0, "files": 0}
    with _read_ledger(path, dry_run=dry_run) as conn:
        where, params = (" WHERE campaign_id=?", (campaign_id,)) if campaign_id else ("", ())
        rows = conn.execute(
            "SELECT campaign_id,cohort_id,provider,parent_session_id,scope,frozen_end,read_offset,status,"
            "json_extract(metadata_json,'$.source_family') AS source_family,"
            "COALESCE(json_extract(metadata_json,'$.selection.eligible_chars'),0) AS eligible_chars,"
            "COALESCE(json_extract(metadata_json,'$.selection.selected_chars'),0) AS selected_chars,"
            "COALESCE(json_extract(metadata_json,'$.selection.sampled_out_chars'),0) AS sampled_out_chars,"
            "COALESCE(json_extract(metadata_json,'$.selection.gap_count'),0) AS coverage_gaps "
            "FROM backfill_inventory" + where, params).fetchall()
        reservations = conn.execute("SELECT stage,COUNT(*) AS calls,SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens FROM backfill_reservations" + where + " GROUP BY stage", params).fetchall()
    dispositions: dict[str, int] = {}
    projects: dict[str, dict[str, int]] = {}
    for row in rows:
        dispositions[row["status"]] = dispositions.get(row["status"], 0) + 1
        bucket = projects.setdefault(row["scope"] or "unresolved", {"files": 0, "frozen_bytes": 0, "scanned_bytes": 0})
        bucket["files"] += 1
        bucket["frozen_bytes"] += row["frozen_end"]
        bucket["scanned_bytes"] += row["read_offset"]
    sessions = {(r["campaign_id"], r["provider"], r["parent_session_id"]) for r in rows if r["scope"]}
    terminal = {"applied", "no_signal", "duplicate_copy", "complete_sampled"}
    states_by_session: dict[tuple, set] = {}
    for row in rows:
        if row["scope"]:
            states_by_session.setdefault((row["campaign_id"], row["provider"], row["parent_session_id"]), set()).add(row["status"])
    completed = {key for key, states in states_by_session.items() if states <= terminal}
    sources: dict[str, dict[str, int]] = {}
    for label_key, buckets in (("scope", projects), ("source_family", sources)):
        for row in rows:
            label = row[label_key] or (row["provider"] if label_key == "source_family" else "unresolved")
            bucket = buckets.setdefault(label, {"files": 0, "frozen_bytes": 0, "scanned_bytes": 0})
            if label_key == "source_family":
                bucket["files"] += 1
                bucket["frozen_bytes"] += row["frozen_end"]
                bucket["scanned_bytes"] += row["read_offset"]
        for label, bucket in buckets.items():
            keys = {(r["campaign_id"], r["provider"], r["parent_session_id"]) for r in rows
                    if (r[label_key] or (r["provider"] if label_key == "source_family" else "unresolved")) == label and r["scope"]}
            bucket["sessions"] = len(keys)
            bucket["completed_sessions"] = len(keys & completed)
    guard_path = state_dir / GUARD_NAME
    guards = json.loads(guard_path.read_text(encoding="utf-8")) if guard_path.exists() else {}
    pauses = {key: value["state"] for key, value in guards.items()
              if (campaign_id is None or key.startswith(campaign_id + ":")) and value.get("state") != "running"}
    return {"state": "inventoried", "files": len(rows), "sessions": len(sessions),
            "completed_sessions": len(completed), "progress": len(completed) / len(sessions) if sessions else 0,
            "dispositions": dispositions, "projects": projects, "sources": sources,
            "eligible_chars": sum(r["eligible_chars"] for r in rows),
            "selected_chars": sum(r["selected_chars"] for r in rows),
            "sampled_out_chars": sum(r["sampled_out_chars"] for r in rows),
            "coverage_gaps": sum(r["coverage_gaps"] for r in rows),
            "pause_latches": pauses, "eta_seconds": None,
            "usage_reservations": [dict(r) for r in reservations], "coverage_contract": "selector_v2_sampled"}


def _session_bundles(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Keep parent framing first, then order whole sessions by priority/time."""
    sessions: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        sessions.setdefault((row["provider"], row["parent_session_id"]), []).append(row)
    for bundle in sessions.values():
        bundle.sort(key=lambda row: (row["metadata"].get("source_family") == "claude_subagent",
                                     row.get("internal_timestamp") or "", row["stream_id"]))
    leaders = {bundle[0]["stream_id"]: bundle for bundle in sessions.values()}
    return [leaders[row["stream_id"]] for row in order_inventory([bundle[0] for bundle in sessions.values()])]


class BackfillRunner:
    def __init__(self, config: BackfillConfig, *, extractor=None, consolidator=None,
                 reference_service_factory: Callable[[], Any] | None = None,
                 application_service_factory: Callable[[], Any] | None = None,
                 haiku=None, sonnet=None, guard: Callable[[str], Any] | None = None,
                 active_probes: Callable[[], dict[str, Any]] | None = None,
                 now: Callable[[], datetime] | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.config = config
        self.extractor, self.consolidator = extractor, consolidator
        self.reference_service_factory = reference_service_factory
        self.application_service_factory = application_service_factory
        self.haiku, self.sonnet = haiku, sonnet
        self.guard, self.active_probes = guard, active_probes
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self._ledger: DreamLedger | None = None
        self._lease_owner: str | None = None

    def _hold_lease(self) -> None:
        if self._lease_owner is not None and not self.ledger.renew_lease(
                "backfill-runner", self._lease_owner, 3600, now=self.now()):
            raise RuntimeError("backfill lease lost")

    @property
    def ledger(self) -> DreamLedger:
        if self._ledger is None:
            self._ledger = DreamLedger(self.config.state_dir / LEDGER_NAME)
        return self._ledger

    def _rows(self) -> list[dict[str, Any]]:
        with self.ledger._connect() as conn:
            rows = conn.execute("SELECT * FROM backfill_inventory WHERE campaign_id=?", (self.config.campaign_id,)).fetchall()
        return [{**dict(row), "metadata": json.loads(row["metadata_json"])} for row in rows]

    def _save(self, row: dict[str, Any]) -> None:
        self._hold_lease()
        metadata = json.dumps(row["metadata"], ensure_ascii=False)
        if len(metadata.encode()) > 512_000:
            raise ValueError("inventory metadata exceeds bound")
        with self.ledger._connect() as conn:
            conn.execute("UPDATE backfill_inventory SET status=?,read_offset=?,metadata_json=? WHERE campaign_id=? AND stream_id=?",
                         (row["status"], row["read_offset"], metadata, self.config.campaign_id, row["stream_id"]))

    def inventory(self, *, dry_run: bool = False, metadata: Mapping[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        from memorymaster.core.scope_utils import resolve_transcript_workspace_scope

        rows: list[dict[str, Any]] = []
        for provider, root in self.config.roots.items():
            root = Path(root)
            agy_sessions = {}
            if provider == "agy":
                # Exact compact path only; full/chunks never become new evidence streams.
                from memorymaster.dreaming.agy_source import discover_agy_sessions

                agy_sessions = {session.transcript_path: session for session in discover_agy_sessions(root)}
                paths = sorted(agy_sessions)
            else:
                paths = sorted(root.rglob("*.jsonl"))
            for path in paths:
                session, cwd, stamp, generated = _header(path, provider)
                extra = (metadata or {}).get(str(path), {})
                family = provider
                if provider == "claude" and path.parent.name == "subagents":
                    family = "claude_subagent"
                    session = path.parent.parent.name
                    parent = path.parent.parent.parent / (session + ".jsonl")
                    if not cwd and parent.is_file():
                        _, cwd, _, _ = _header(parent, provider)
                if provider == "agy":
                    session = path.parents[2].name
                    cwd = extra.get("cwd")
                    if not dry_run and not cwd:
                        from memorymaster.dreaming.agy_source import read_executor_metadata

                        native = read_executor_metadata(agy_sessions[path].database_path or root / "missing-companion.db")
                        cwd = native.cwd
                cwd = extra.get("cwd") or cwd
                generated |= bool(cwd and ("provider-slot-" in str(cwd) or "dreaming-opencode" in str(cwd)))
                if generated:
                    continue
                end, digest = frozen_identity(path)
                scope = resolve_transcript_workspace_scope(cwd)
                stream_id = fingerprint([provider, str(path.resolve())])
                parent = fingerprint([provider, session])
                rows.append({"campaign_id": self.config.campaign_id, "stream_id": stream_id,
                             "provider": provider, "parent_session_id": parent, "source_locator": str(path.resolve()),
                             "frozen_end": end, "source_digest": digest, "scope": scope,
                             "internal_timestamp": stamp, "status": "inventoried" if scope else "unresolved_scope",
                             "read_offset": 0, "metadata": {"scope_cwd": cwd, "scope_resolution": "git-alias-v2",
                                 "source_family": family, "source_session_id": session,
                                 "unknown_date": stamp is None, "agy_metadata_pending": provider == "agy" and dry_run and not cwd}})
                if provider == "agy" and agy_sessions[path].full_path:
                    full = agy_sessions[path].full_path
                    full_end, full_digest = frozen_identity(full)
                    rows[-1]["metadata"]["full_source"] = {"locator": str(full), "end": full_end, "digest": full_digest}
        for bundle in _session_bundles(rows):
            if len({row["scope"] for row in bundle if row["scope"]}) > 1:
                for row in bundle:
                    row["scope"], row["status"] = None, "unresolved_scope"
                    row["metadata"]["scope_resolution"] = "conflicting_bundle_scope"
        bundles = _session_bundles(rows)
        rows = [row for bundle in bundles for row in bundle]
        cohorts: dict[tuple[str, str], str] = {}
        counts: dict[str, int] = {}
        copies: set[tuple[str, str, str]] = set()
        for row in rows:
            provider = row["provider"]
            key = (provider, row["parent_session_id"])
            if key not in cohorts:
                n = counts.get(provider, 0)
                index = 0 if n < self.config.pilot_sessions else 1 + (n - self.config.pilot_sessions) // self.config.cohort_sessions
                cohorts[key] = f"{provider}-{index}"
                counts[provider] = n + 1
            row["cohort_id"] = cohorts[key]
            copy = (provider, row["parent_session_id"], row["source_digest"])
            if copy in copies:
                row["status"] = "duplicate_copy"
            copies.add(copy)
        if not dry_run:
            with self.ledger._connect() as conn:
                frozen_campaign = conn.execute("SELECT 1 FROM backfill_inventory WHERE campaign_id=? LIMIT 1", (self.config.campaign_id,)).fetchone() is not None
                for row in rows:
                    previous = conn.execute("SELECT source_digest,frozen_end FROM backfill_inventory WHERE campaign_id=? AND stream_id=?", (self.config.campaign_id, row["stream_id"])).fetchone()
                    if previous:
                        # Appends belong to the next campaign, never reset frozen progress.
                        if not verify_frozen(Path(row["source_locator"]), previous["frozen_end"], previous["source_digest"]):
                            conn.execute("UPDATE backfill_inventory SET status='invalidated' WHERE campaign_id=? AND stream_id=?", (self.config.campaign_id, row["stream_id"]))
                        continue
                    if frozen_campaign:
                        # Newly discovered streams belong to a new generation.
                        # Never reorder or expand an already frozen cohort.
                        continue
                    conn.execute("INSERT INTO backfill_inventory(campaign_id,stream_id,cohort_id,provider,parent_session_id,source_locator,frozen_end,source_digest,scope,internal_timestamp,status,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                 tuple(row[k] for k in ("campaign_id", "stream_id", "cohort_id", "provider", "parent_session_id", "source_locator", "frozen_end", "source_digest", "scope", "internal_timestamp", "status")) + (json.dumps(row["metadata"]),))
        # Reports never contain source paths/cwd or transcript text.
        plan = {"windows": 0, "eligible_chars": 0, "selected_chars": 0, "sampled_out_chars": 0, "coverage_gaps": 0}
        if dry_run:
            for bundle in bundles:
                if not bundle[0]["scope"]:
                    continue
                gaps = []
                selected = select_windows((turn for row in bundle for turn in self._turns(row, gaps)), scope=bundle[0]["scope"])
                plan["windows"] += len(selected["windows"])
                for key in ("eligible_chars", "selected_chars", "sampled_out_chars"):
                    plan[key] += selected[key]
                plan["coverage_gaps"] += len(gaps)
        return {"dry_run": dry_run, "files": len(rows), "sessions_by_source": counts,
                "frozen_bytes": sum(r["frozen_end"] for r in rows),
                "unresolved": sum(r["scope"] is None for r in rows),
                "unknown_date": sum(r["internal_timestamp"] is None for r in rows),
                "max_extraction_calls": len(cohorts) * 4,
                "ordering": [dict(scope=r["scope"], source=r["provider"], timestamp=r["internal_timestamp"], stream_id=r["stream_id"]) for r in rows],
                "coverage_contract": "selector_v2_sampled", "offline_selection": plan if dry_run else None,
                "input_token_ceiling": len(cohorts) * 4 * 10_000,
                "output_token_ceiling": len(cohorts) * 4 * 1500}

    def _latches(self) -> dict[str, Any]:
        path = self.config.state_dir / GUARD_NAME
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def pause(self, *, cohort_id: str = "campaign", state: str = "operator_paused", dry_run: bool = False) -> dict[str, Any]:
        states = self._latches()
        key = self.config.campaign_id + ":" + cohort_id
        states[key] = asdict(PauseLatch(state=state))
        if not dry_run:
            _atomic_json(self.config.state_dir / GUARD_NAME, states)
        return {"state": state, "cohort": cohort_id, "dry_run": dry_run}

    def resume(self, *, cohort_id: str = "campaign", evidence: dict[str, Any] | None = None, dry_run: bool = False) -> dict[str, Any]:
        states = self._latches()
        key = self.config.campaign_id + ":" + cohort_id
        latch = PauseLatch(**states.get(key, {}))
        latch.resume(evidence=evidence or {}, now=self.now().timestamp())
        states[key] = asdict(latch)
        for stage_key in list(states):
            if stage_key.startswith(key + ":"):
                stage_latch = PauseLatch(**states[stage_key])
                stage_latch.resume(evidence=evidence or {}, now=self.now().timestamp())
                states[stage_key] = asdict(stage_latch)
        if not dry_run:
            _atomic_json(self.config.state_dir / GUARD_NAME, states)
        return {"state": latch.state, "cohort": cohort_id, "dry_run": dry_run}

    def _allowed(self, cohort: str, stage: str) -> bool:
        from memorymaster.dreaming.latency_guard import GuardDecision

        self._hold_lease()
        states = self._latches()
        keys = [self.config.campaign_id + ":campaign", self.config.campaign_id + ":" + cohort]
        if any(states.get(key, {}).get("state", "running") in {"operator_paused", "quality_paused", "budget_paused", "latency_paused_manual", "retired"} for key in keys):
            return False
        stage_key = keys[1] + ":" + stage
        if states.get(stage_key, {}).get("state") in {"latency_paused_manual", "retired"}:
            return False
        try:
            passive = self.guard(stage) if self.guard else GuardDecision("allow", ["insufficient_data"])
            if not isinstance(passive, GuardDecision):
                passive = GuardDecision("allow", ["malformed_monitoring"])
        except Exception as exc:
            passive = GuardDecision("allow", ["guard_error:" + type(exc).__name__])
        try:
            active = self.active_probes() if self.active_probes and (stage != "extract" or states.get(stage_key, {}).get("state") == "latency_paused") else None
        except Exception:
            active = {"passed": False, "checked_at": self.now().timestamp(), "channels": {}}
        decision = evaluate_backfill_guard(passive, stage=stage, active=active, now=self.now().timestamp())
        latch = PauseLatch(**states.get(stage_key, {}))
        state = latch.observe(decision, self.now().timestamp())
        latch.evidence = {"stage": stage, "checked_at": self.now().timestamp(), "decision": decision.action,
                          "reasons": decision.reasons,
                          "passive": {name: {key: (passive.metrics.get(name) if isinstance(passive.metrics.get(name), dict) else {}).get(key) for key in ("n", "p95_ms")}
                                      for name in ("dense", "hook")},
                          "active": {"passed": bool(active and active.get("passed")),
                                     "checked_at": active.get("checked_at") if active else None,
                                     "mapping": {"dense": "dense_search", "recall": "authorized_sqlite_recall"},
                                     "thresholds_ms": {"dense": [150, 300], "hook": [900, 1200], "recall": [900, 950]}}}
        states[stage_key] = asdict(latch)
        _atomic_json(self.config.state_dir / GUARD_NAME, states)
        return decision.action == "allow" and state == "running"

    def _reserve(self, cohort: str, stage: str, input_tokens: int, output_tokens: int) -> str:
        self._hold_lease()
        request_caps = {"extract": (10_000, 1500), "consolidate": (30_000, 4000),
                        "apply": (0, 0), "haiku": (2000, 300), "sonnet": (2000, 300), "missed_signal": (4000, 300)}
        if (stage not in request_caps or type(input_tokens) is not int or type(output_tokens) is not int
                or input_tokens < 0 or output_tokens < 0
                or input_tokens > request_caps[stage][0] or output_tokens > request_caps[stage][1]):
            raise ValueError("request exceeds approved reservation cap")
        day = self.now().astimezone(timezone.utc).date().isoformat()
        caps = {"extract": (self.config.max_extract_daily, 12_000_000, 1_800_000),
                "consolidate": (self.config.max_consolidate_daily, 6_000_000, 800_000),
                "apply": (self.config.max_apply_daily, 0, 0),
                "haiku": (100, 200_000, 30_000), "sonnet": (20, 40_000, 6_000),
                "missed_signal": (40, 160_000, 12_000)}
        max_calls, max_in, max_out = caps[stage]
        reservation = uuid.uuid4().hex
        with self.ledger._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            judge = stage in {"haiku", "sonnet", "missed_signal"}
            where, params = ("campaign_id=? AND cohort_id=?", (self.config.campaign_id, cohort)) if judge else ("utc_day=?", (day,))
            usage = conn.execute("SELECT COUNT(*),COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0) FROM backfill_reservations WHERE stage=? AND " + where + " AND status!='released'", (stage, *params)).fetchone()
            if usage[0] >= max_calls or usage[1] + input_tokens > max_in or usage[2] + output_tokens > max_out:
                raise RuntimeError("daily_budget_deferred")
            if stage == "consolidate":
                cohort_usage = conn.execute("SELECT COUNT(*),COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0) FROM backfill_reservations WHERE campaign_id=? AND cohort_id=? AND stage=? AND status!='released'", (self.config.campaign_id, cohort, stage)).fetchone()
                if cohort_usage[0] >= self.config.max_consolidate_cohort or cohort_usage[1] + input_tokens > 12_000_000 or cohort_usage[2] + output_tokens > 1_600_000:
                    self.pause(cohort_id=cohort, state="budget_paused")
                    raise RuntimeError("cohort_budget_paused")
            conn.execute("INSERT INTO backfill_reservations VALUES(?,?,?,?,?,?,?,'reserved')", (reservation, self.config.campaign_id, cohort, stage, day, input_tokens, output_tokens))
        return reservation

    def _usage(self, reservation: str, result, run_id: str) -> None:
        usage = result.usage
        if (type(usage.input_tokens) is not int or type(usage.output_tokens) is not int
                or min(usage.input_tokens, usage.output_tokens) < 0):
            raise ValueError("invalid provider usage receipt")
        # A missing/ambiguous usage receipt keeps the maximum reservation charged.
        with self.ledger._connect() as conn:
            cap = conn.execute("SELECT input_tokens,output_tokens FROM backfill_reservations WHERE reservation_id=?", (reservation,)).fetchone()
            if usage.input_tokens > cap[0] or usage.output_tokens > cap[1]:
                self.pause(state="budget_paused")
                raise ValueError("provider exceeded reserved envelope")
            if usage.input_tokens > 0 and usage.output_tokens > 0:
                conn.execute("UPDATE backfill_reservations SET input_tokens=?,output_tokens=?,status='settled' WHERE reservation_id=?", (usage.input_tokens, usage.output_tokens, reservation))
        self.ledger.record_provider_call(run_id, **{key: getattr(usage, key) for key in ("provider", "model", "latency_ms", "structured_valid", "input_tokens", "output_tokens", "http_status")}, outcome="success", now=self.now())

    def _release_judge_reservations(self, cohort: str) -> None:
        """Renew the evaluation allowance, including every source-family stratum."""
        self._hold_lease()
        prefix = cohort + "/"
        with self.ledger._connect() as conn:
            conn.execute(
                "UPDATE backfill_reservations SET status='released' WHERE campaign_id=? "
                "AND (cohort_id=? OR substr(cohort_id,1,?)=?) "
                "AND stage IN ('haiku','sonnet','missed_signal') AND status!='released'",
                (self.config.campaign_id, cohort, len(prefix), prefix),
            )

    def _scope_valid(self, row: dict[str, Any]) -> bool:
        from memorymaster.core.scope_utils import resolve_transcript_workspace_scope

        return row["scope"] and resolve_transcript_workspace_scope(row["metadata"].get("scope_cwd")) == row["scope"]

    def _source_valid(self, row: dict[str, Any]) -> bool:
        try:
            if not verify_frozen(Path(row["source_locator"]), row["frozen_end"], row["source_digest"]):
                return False
            full = row["metadata"].get("full_source")
            return not full or verify_frozen(Path(full["locator"]), full["end"], full["digest"])
        except OSError:
            return False

    def _turns(self, row: dict[str, Any], gaps: list[dict[str, Any]]):
        if row["provider"] != "agy":
            yield from iter_source_turns(Path(row["source_locator"]), row["provider"], row["stream_id"], row["frozen_end"], gaps)
            return
        from memorymaster.dreaming.agy_source import iter_agy_records
        from memorymaster.dreaming.backfill_selection import SourceTurn

        full = row["metadata"].get("full_source")
        # A companion created after inventory cannot become frozen evidence.
        missing = Path(row["source_locator"]).with_name("ao7-missing-full.jsonl")
        for record in iter_agy_records(row["source_locator"], frozen_end=row["frozen_end"],
                                       full_path=full["locator"] if full else missing,
                                       full_frozen_end=full["end"] if full else 0):
            yield SourceTurn(record.message, record.start_offset, record.end_offset, row["stream_id"])

    def _cohort_complete(self, rows: list[dict[str, Any]], *, decisions: bool = False) -> bool:
        owners = {row["stream_id"]: row for row in rows if "selection" in row["metadata"]}
        if not owners or any(row["metadata"].get("selection_owner") not in owners for row in rows):
            return False
        for owner in owners.values():
            for window in owner["metadata"]["selection"]["windows"]:
                capture = self.ledger.get_capture(window["capture_id"]) if window.get("capture_id") else None
                if capture is None or capture.get("extraction") is None:
                    return False
                if decisions and (capture.get("decisions") is None or
                                  {c["candidate_id"] for c in capture["extraction"]} !=
                                  {d["candidate_id"] for d in capture["decisions"]}):
                    return False
        return True

    def _select(self, rows: list[dict[str, Any]]) -> None:
        leader = rows[0]
        if "selection" in leader["metadata"]:
            for row in rows:
                if "selection_owner" not in row["metadata"]:
                    row["metadata"]["selection_owner"] = leader["stream_id"]
                    row["read_offset"] = row["frozen_end"]
                    row["status"] = "selected" if leader["metadata"]["selection"]["windows"] else "no_signal"
                    self._save(row)
            return
        gaps: list[dict[str, Any]] = []

        def turns():
            for row in rows:
                self._hold_lease()
                yield from self._turns(row, gaps)

        selection = select_windows(turns(), scope=leader["scope"])
        # Expose omitted byte intervals in each physical stream as well as text coverage.
        for row in rows:
            selected = sorted((r["start"], r["end"]) for w in selection["windows"] for r in w["ranges"] if r["stream"] == row["stream_id"])
            start = 0
            for left, right in selected:
                if left > start:
                    gaps.append({"stream": row["stream_id"], "start": start, "end": left, "reason": "sampled_out"})
                start = max(start, right)
            if start < row["frozen_end"]:
                gaps.append({"stream": row["stream_id"], "start": start, "end": row["frozen_end"], "reason": "sampled_out"})
        selection["gaps"] = gaps[:200]  # counts remain exact; manifests are bounded.
        selection["gap_count"] = len(gaps)
        for index, window in enumerate(selection["windows"]):
            counter = getattr(self.extractor, "rendered_input_tokens", None)
            while counter and window["messages"] and counter(window["messages"], scope=leader["scope"]) > 10_000:
                message = window["messages"][-1]
                if len(message["text"]) > 200:
                    before = len(message["text"])
                    message["text"] = message["text"][:before // 2]
                    message["message_id"] += f":span-0-{before // 2}"
                else:
                    window["messages"].pop()
                    window["ranges"].pop()
            if not window["messages"]:
                raise ValueError("route framing alone exceeds extraction allowance")
            window["identity"] = fingerprint([leader["provider"], leader["parent_session_id"], window])
            window["index"] = index
        selection["selected_chars"] = sum(len(m["text"]) for w in selection["windows"] for m in w["messages"])
        selection["sampled_out_chars"] = selection["eligible_chars"] - selection["selected_chars"]
        leader["metadata"]["selection"] = selection
        for row in rows:
            row["metadata"]["selection_owner"] = leader["stream_id"]
            row["read_offset"] = row["frozen_end"]
            row["status"] = "selected" if selection["windows"] else "no_signal"
            self._save(row)

    def _extract(self, row: dict[str, Any], run_id: str) -> None:
        if self.extractor is None:
            raise ValueError("explicit extractor required")
        for window in row["metadata"]["selection"]["windows"]:
            if window.get("capture_id"):
                continue
            if window.get("attempts", 0) >= 3:
                row["status"] = "unresolved_extraction"
                self._save(row)
                continue
            if not self._allowed(row["cohort_id"], "extract"):
                return
            if window.get("attempts", 0):
                all_rows = [item for item in self._rows() if item["cohort_id"] == row["cohort_id"]]
                planned = [w for item in all_rows for w in item["metadata"].get("selection", {}).get("windows", [])]
                repairs = sum(max(0, w.get("attempts", 0) - 1) for w in planned)
                if repairs >= max(1, -(-len(planned) // 10)):
                    raise RuntimeError("extraction_repair_allowance_exhausted")
            # Actual rendered preflight is enforced by the configured provider adapter.
            messages = window["messages"]
            counter = getattr(self.extractor, "rendered_input_tokens", None)
            if counter and counter(messages, scope=row["scope"]) > 10_000:
                raise ValueError("selected window exceeds rendered transport cap")
            reservation = self._reserve(row["cohort_id"], "extract", 10_000, 1500)
            window["attempts"] = window.get("attempts", 0) + 1
            self._save(row)
            try:
                result = self.extractor.extract(messages, scope=row["scope"], capture_hash=window["identity"])
            except ProviderCallError as exc:
                # T-1722: a malformed or failed provider reply costs this window one attempt (three
                # strikes make it unresolved_extraction, repairs count toward the allowance); the
                # run goes on to the next window instead of aborting the whole campaign.
                window["last_error"] = type(exc).__name__
                self._save(row)
                continue
            window.pop("last_error", None)
            self._usage(reservation, result, run_id)
            if len(result.candidates) > 5:
                raise ValueError("extractor exceeded candidate cap")
            stamp = self.now().isoformat()
            envelope = CaptureEnvelope(row["provider"], row["parent_session_id"], row["scope"], stamp,
                                       row["internal_timestamp"] or stamp,
                                       tuple(DreamMessage(**m) for m in messages), window["index"], window["index"] + 1,
                                       window["identity"])
            capture_id = self.ledger.enqueue(envelope, coalesce=False)
            self.ledger.set_extraction(capture_id, [c.to_dict() for c in result.candidates], run_id)
            window["capture_id"] = capture_id
            self._save(row)
        if all(w.get("capture_id") for w in row["metadata"]["selection"]["windows"]):
            row["status"] = "extracted" if row["metadata"]["selection"]["windows"] else "no_signal"
            self._save(row)

    def _worker(self, service):
        from memorymaster.dreaming.worker import DreamConfig, DreamWorker

        config = DreamConfig(idle_minutes=0, jev_triage_enabled=False, max_context_chars=60_000,
                             max_consolidate_candidates=5, max_candidate_writes_daily=200)
        return DreamWorker(self.ledger, service, self.extractor, self.consolidator, config=config, now=self.now)

    def _captures(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.ledger.get_capture(w["capture_id"]) for row in rows
                for w in row["metadata"].get("selection", {}).get("windows", []) if w.get("capture_id")]

    def _consolidate(self, rows: list[dict[str, Any]], run_id: str) -> None:
        if not self._cohort_complete(rows):
            raise ValueError("incomplete extraction cohort cannot consolidate")
        if not self.reference_service_factory or self.consolidator is None:
            raise ValueError("explicit read-only reference service and consolidator required")
        service = self.reference_service_factory()
        _require_sqlite_service(service)
        if not getattr(service.store, "read_only", False):
            raise ValueError("reference service must be read-only")
        worker = self._worker(service)
        from memorymaster.dreaming.source_review import bind_source

        captures = self._captures(rows)
        groups: dict[str, list] = {}
        decisions: dict[int, list[dict[str, Any]]] = {}
        for capture in captures:
            for raw in capture.get("extraction") or []:
                candidate = DreamCandidate(**raw)
                scope = "personal" if candidate.scope_class == "personal" else capture["scope"]
                groups.setdefault(scope, []).append((capture, bind_source(candidate, capture)))
            decisions[capture["id"]] = []
        own_ids = self._owned_ids(rows)
        snapshots = {scope: [item for item in worker._current_claims(scope, [c for _, c in pairs]) if item["id"] not in own_ids]
                     for scope, pairs in groups.items()}
        hashes = {scope: fingerprint(claims) for scope, claims in snapshots.items()}
        prior = rows[0]["metadata"].get("reference_hashes")
        if prior is not None and prior != hashes:
            # References changed: retain extraction but renew decisions and M5.
            with self.ledger._connect() as conn:
                for capture in captures:
                    conn.execute("UPDATE dream_captures SET decisions_json=NULL,state='extracted' WHERE id=?", (capture["id"],))
                    capture["decisions"] = None
            self._release_judge_reservations(rows[0]["cohort_id"])
            for row in rows:
                row["metadata"].pop("quality_receipt", None)
                row["metadata"].pop("quality_receipts", None)
                row["metadata"].pop("discarded_audit", None)
        rows[0]["metadata"]["reference_hashes"] = hashes
        self._save(rows[0])
        existing = {capture["id"] for capture in captures if capture.get("decisions") is not None}
        decisions = {key: value for key, value in decisions.items() if key not in existing}
        for scope, pairs in groups.items():
            pairs = [pair for pair in pairs if pair[0]["id"] not in existing]
            batches = list(worker._candidate_batches(pairs))
            while batches:
                batch = batches.pop(0)
                cohort = rows[0]["cohort_id"]
                if not self._allowed(cohort, "consolidate"):
                    return
                candidates = [candidate for _, candidate in batch]
                claims = snapshots[scope]
                try:
                    fitted = worker._fit_prompt(candidates, claims, scope)
                    if len(batch) > 1 and len(fitted) < len(claims):
                        split_at = next((n for n in range(len(batch) - 1, 0, -1)
                                         if len(worker._fit_prompt(candidates[:n], claims, scope)) > len(fitted)), None)
                        if split_at is not None:
                            batches[0:0] = [batch[:split_at], batch[split_at:]]
                            continue
                    claims = fitted
                except Exception:
                    if len(batch) > 1:
                        batches[0:0] = [[pair] for pair in batch]
                        continue
                    raise
                identity = fingerprint([scope, [c.candidate_id for c in candidates], hashes[scope]])
                attempts = rows[0]["metadata"].setdefault("consolidation_attempts", {})
                if attempts.get(identity, 0) >= 3:
                    raise ValueError("consolidation repair attempts exhausted")
                reservation = self._reserve(cohort, "consolidate", 30_000, 4000)
                attempts[identity] = attempts.get(identity, 0) + 1
                self._save(rows[0])
                result = self.consolidator.consolidate(candidates, claims, scope=scope)
                self._usage(reservation, result, run_id)
                reviewed, _ = worker._reviewed_batch(result, candidates, scope)
                by_id = {d.candidate_id: d for d in reviewed}
                for capture, candidate in batch:
                    decisions[capture["id"]].append(by_id[candidate.candidate_id].to_dict())
        for capture_id, payloads in decisions.items():
            self.ledger.set_decisions(capture_id, payloads, run_id)
        for row in rows:
            row["status"] = "consolidated"
            self._save(row)

    @staticmethod
    def _owned_ids(rows: list[dict[str, Any]]) -> set[int]:
        return {claim_id for row in rows for entry in row["metadata"].get("applications", {}).values()
                for claim_id in entry.get("created_claim_ids", [])}

    def _population(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from memorymaster.dreaming.source_review import bind_source

        population = []
        for capture in self._captures(rows):
            accepted = {d["candidate_id"]: d for d in capture.get("decisions") or [] if d["action"] not in {"ignore", "reinforce"}}
            for candidate in capture.get("extraction") or []:
                if candidate["candidate_id"] in accepted:
                    family = capture["provider"]
                    for row in rows:
                        for window in row["metadata"].get("selection", {}).get("windows", []):
                            if window.get("capture_id") != capture["id"]:
                                continue
                            index = next((i for i, m in enumerate(window["messages"]) if m["message_id"] == candidate["evidence_message_id"]), 0)
                            origin = window["ranges"][index]["stream"]
                            family = next(r["metadata"]["source_family"] for r in rows if r["stream_id"] == origin)
                    bound = bind_source(DreamCandidate(**candidate), capture)
                    population.append({**bound.to_dict(), "decision": accepted[candidate["candidate_id"]],
                                       "capture_hash": capture["content_hash"], "source_family": family})
        return population

    def _discarded(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cached = rows[0]["metadata"].get("discarded_audit")
        if cached is not None:
            return cached
        audit: list[dict[str, Any]] = []
        for row in rows:
            for window in row["metadata"].get("selection", {}).get("windows", []):
                capture = self.ledger.get_capture(window["capture_id"])
                if not capture["extraction"] or any(d["action"] == "ignore" for d in capture.get("decisions") or []):
                    audit.append({"selection_id": window["identity"], "scope": row["scope"],
                                  "reason": "discarded_or_no_signal", "messages": window["messages"],
                                  "source_family": row["metadata"]["source_family"]})
        # Choose independent source selections first, then inspect omitted authored
        # evidence. Never substitute the kept-window text for sampled-out material.
        rng = random.Random(fingerprint(self._identity(rows)))
        selected = {m["message_id"]: m["text"] for row in rows
                    for w in row["metadata"].get("selection", {}).get("windows", []) for m in w["messages"]}
        for row in rng.sample(rows, min(40, len(rows))):
            for turn in self._turns(row, []):
                message = turn.message
                kept = selected.get(message.message_id)
                if kept is None:
                    kept = next((text for key, text in selected.items() if key.startswith(message.message_id + ":span-")), None)
                omitted = message.text[len(kept):] if kept is not None and message.text.startswith(kept) else message.text if kept is None else ""
                if not omitted:
                    continue
                audit.append({"selection_id": fingerprint([row["stream_id"], turn.start, turn.end, len(message.text) - len(omitted)]),
                              "scope": row["scope"], "reason": "sampled_out", "messages": [{**message.to_dict(), "text": omitted[:4000]}],
                              "source_family": row["metadata"]["source_family"]})
                break
        audit = rng.sample(audit, min(40, len(audit)))
        rows[0]["metadata"]["discarded_audit"] = audit
        self._save(rows[0])
        return audit

    def _identity(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {"campaign": self.config.campaign_id, "cohort": rows[0]["cohort_id"],
                "sources": fingerprint(sorted([r["stream_id"], r["source_digest"], r["frozen_end"], r["scope"]] for r in rows)),
                "filter": "v2", "selector": "v2", "rubric": self.config.rubric_version,
                "extractor": self.extractor.model if self.extractor else self.config.extractor_model,
                "consolidator": self.consolidator.model if self.consolidator else self.config.consolidator_model,
                "judges": [[getattr(judge, "provider", None), getattr(judge, "model", None)] for judge in (self.haiku, self.sonnet)],
                "selections": fingerprint([row["metadata"].get("selection") for row in rows])}

    def _quality(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not self._cohort_complete(rows, decisions=True):
            raise ValueError("incomplete cohort cannot pass quality")
        if self.haiku is None or self.sonnet is None:
            raise ValueError("explicit independent Haiku/Sonnet judges required")
        population = self._population(rows)
        existing = rows[0]["metadata"].get("quality_receipt", {})
        if existing and not existing.get("complete"):
            rows[0]["metadata"].pop("discarded_audit", None)
        discarded = self._discarded(rows)
        identity = self._identity(rows)
        if receipt_is_current(existing, population, identity, discarded=discarded):
            return existing
        # Caps bound one M5 evaluation, rather than the cohort's replay lifetime.
        # Keep provider-call history while releasing superseded reservations.
        self._release_judge_reservations(rows[0]["cohort_id"])
        families = sorted({r["metadata"]["source_family"] for r in rows})
        strata = {}
        for family in families:
            strata[family] = evaluate_cohort_quality(
                [c for c in population if c["source_family"] == family],
                [d for d in discarded if d["source_family"] == family], haiku=self.haiku, sonnet=self.sonnet,
                identity={**identity, "stratum": family}, seed=fingerprint([identity, family]),
                reserve=lambda stage, i, o: self._reserve(rows[0]["cohort_id"] + "/" + family, stage, i, o),
                settle=self._settle_judge)
        passed = all(r["passed"] for r in strata.values())
        receipt = {"passed": passed, "complete": all(r["complete"] for r in strata.values()),
                   "state": "released" if passed else "quality_paused", "strata": strata,
                   "identity": {**identity, "population_hash": fingerprint(sorted(population, key=lambda c: str(c["candidate_id"]))),
                                "discarded_hash": fingerprint(sorted(discarded, key=lambda d: str(d["selection_id"])))},
                   "known_junk_ids": sorted({key for r in strata.values() for key in r["known_junk_ids"]}),
                   "pause_scope": "campaign" if any(r["pause_scope"] == "campaign" for r in strata.values()) else "cohort"}
        receipt["receipt_hash"] = fingerprint(receipt)
        rows[0]["metadata"]["quality_receipt"] = receipt
        self._save(rows[0])
        if not receipt["passed"]:
            self.pause(cohort_id="campaign" if receipt["pause_scope"] == "campaign" else rows[0]["cohort_id"], state="quality_paused")
        return receipt

    def _settle_judge(self, reservation: str, receipt: dict[str, Any]) -> None:
        from types import SimpleNamespace

        self._usage(reservation, SimpleNamespace(usage=SimpleNamespace(
            provider=receipt["provider"], model=receipt["observed_model"], latency_ms=0, structured_valid=True,
            input_tokens=receipt["input_tokens"], output_tokens=receipt["output_tokens"], http_status=200)), self._run_id)

    def run(self, *, stage: str = "extract", apply_candidates: bool = False, dry_run: bool = False,
            cohort_id: str | None = None) -> dict[str, Any]:
        if stage not in {"extract", "consolidate", "quality", "apply", "all"}:
            raise ValueError("unknown stage")
        if dry_run:
            return self.inventory(dry_run=True)
        owner = uuid.uuid4().hex
        if not self.ledger.acquire_lease("backfill-runner", owner, 3600, now=self.now()):
            return {"state": "leased"}
        self._lease_owner = owner
        run_id = self.ledger.start_run(not apply_candidates, getattr(self.extractor, "model", ""), getattr(self.consolidator, "model", ""), now=self.now())
        self._run_id = run_id
        try:
            bundles = _session_bundles([r for r in self._rows() if r["scope"] and r["status"] != "duplicate_copy"
                                       and (cohort_id is None or r["cohort_id"] == cohort_id)])
            rows = [row for bundle in bundles for row in bundle]
            # A changed frozen source invalidates all downstream receipts for its cohort.
            for row in rows:
                if not self._source_valid(row) or not self._scope_valid(row):
                    row["status"] = "invalidated"
                    self._save(row)
            if any(r["status"] == "invalidated" for r in rows):
                raise ValueError("source or scope changed; new campaign required")
            cohorts: dict[str, list] = {}
            for row in rows:
                cohorts.setdefault(row["cohort_id"], []).append(row)
            for cohort, group in cohorts.items():
                if not self._allowed(cohort, "extract" if stage in {"extract", "all"} else "consolidate" if stage != "apply" else "apply"):
                    continue
                if stage in {"extract", "all"}:
                    sessions = _session_bundles(group)
                    # Freeze every session's plan before computing the cohort's
                    # shared repair allowance or dispatching any extraction.
                    for bundle in sessions:
                        self._select(bundle)
                    for bundle in sessions:
                        if not self._allowed(cohort, "extract"):
                            break
                        self._extract(bundle[0], run_id)
                    if not self._cohort_complete(group):
                        continue
                if stage in {"consolidate", "all"}:
                    self._consolidate(group, run_id)
                    if not self._cohort_complete(group, decisions=True):
                        continue
                if stage in {"quality", "all"}:
                    if not self._quality(group)["passed"]:
                        continue
                if stage in {"apply", "all"} and apply_candidates:
                    self._apply(group, run_id)
            result = read_backfill_status(self.config.state_dir, campaign_id=self.config.campaign_id)
            _atomic_json(self.config.state_dir / STATUS_NAME, result)
            self.ledger.finish_run(run_id, "completed", result, now=self.now())
            return result
        except Exception:
            self.ledger.finish_run(run_id, "stopped", {"reason": "stage_incomplete"}, now=self.now())
            raise
        finally:
            self.ledger.release_lease("backfill-runner", owner)
            self._lease_owner = None

    def _apply(self, rows: list[dict[str, Any]], run_id: str) -> None:
        if not self._cohort_complete(rows, decisions=True):
            raise ValueError("incomplete cohort cannot apply")
        population = self._population(rows)
        receipt = rows[0]["metadata"].get("quality_receipt", {})
        discarded = self._discarded(rows)
        if not receipt_is_current(receipt, population, self._identity(rows), discarded=discarded):
            raise ValueError("fresh exact cohort release receipt required")
        if not self.application_service_factory:
            raise ValueError("explicit application service required")
        service = self.application_service_factory()
        _require_sqlite_service(service)
        worker = self._worker(service)
        self._reconcile_intents(rows, service)
        # Material authorized reference changes renew consolidation and M5.
        references = rows[0]["metadata"].get("reference_hashes", {})
        grouped: dict[str, list[DreamCandidate]] = {}
        for capture in self._captures(rows):
            for raw in capture["extraction"]:
                candidate = DreamCandidate(**raw)
                scope = "personal" if candidate.scope_class == "personal" else capture["scope"]
                grouped.setdefault(scope, []).append(candidate)
        def verify_references():
            own_ids = self._owned_ids(rows)
            for scope, expected in references.items():
                current = [item for item in worker._current_claims(scope, grouped.get(scope)) if item["id"] not in own_ids]
                if fingerprint(current) != expected:
                    raise ValueError("authorized reference population changed; reconsolidate")

        verify_references()
        excluded = set(receipt["known_junk_ids"])
        attempts = 0
        for row in rows:
            manifest = row["metadata"].setdefault("applications", {})
            for window in row["metadata"].get("selection", {}).get("windows", []):
                capture = self.ledger.get_capture(window["capture_id"])
                candidates = {c["candidate_id"]: DreamCandidate(**c) for c in capture.get("extraction") or []}
                for payload in capture.get("decisions") or []:
                    decision = DreamDecision(**payload)
                    if decision.action in {"ignore", "reinforce"} or decision.candidate_id in excluded:
                        continue
                    key = fingerprint([window["identity"], decision.to_dict()])
                    if manifest.get(key, {}).get("completed"):
                        continue
                    if attempts % 25 == 0:
                        if attempts:
                            self.sleep(.25)
                        if not self._allowed(row["cohort_id"], "apply"):
                            return
                        verify_references()
                        if not all(self._source_valid(source) and self._scope_valid(source) for source in rows):
                            raise ValueError("source or scope changed before application group")
                        if not receipt_is_current(receipt, self._population(rows), self._identity(rows), discarded=discarded):
                            raise ValueError("release receipt changed")
                    if not self._scope_valid(row):
                        raise ValueError("scope changed before application")
                    if not self._source_valid(row):
                        raise ValueError("source changed before application")
                    self._reserve(row["cohort_id"], "apply", 0, 0)
                    candidate = candidates[decision.candidate_id]
                    batch = f"ao7-{self.config.campaign_id}-{row['cohort_id']}-{self._application_ordinal(rows, key) // 200}"
                    operation_id = f"ao7-{self.config.campaign_id}-{row['cohort_id']}-{key}"
                    outcome: dict[str, Any] = {"operation_id": operation_id}
                    pending = manifest.setdefault(key, {"candidate_id": decision.candidate_id,
                                                       "source_hash": window["identity"], "intake_batch_id": batch,
                                                       "operation_id": operation_id,
                                                       "tenant_id": service.tenant_id,
                                                       "scope": "personal" if candidate.scope_class == "personal" else row["scope"],
                                                       "created_claim_ids": [], "reused_claim_ids": [], "proposal_ids": []})
                    self._save(row)  # intent precedes cross-database writes
                    application_row = {**capture, "intake_batch_id": batch, "outcome_receipt": outcome}
                    # Child ranges retain the parent's independent evidence identity.
                    ranges = window["ranges"]
                    message_index = next((i for i, m in enumerate(window["messages"]) if m["message_id"] == candidate.evidence_message_id), 0)
                    origin = ranges[min(message_index, len(ranges) - 1)]["stream"]
                    stream = next((r for r in rows if r["stream_id"] == origin), row)
                    if stream["metadata"].get("source_family") == "claude_subagent":
                        application_row["citation_suffix"] = "subagent-" + stream["stream_id"][:12]
                    summary = {"candidate_writes": 0, "proposals": 0}
                    claim_id = worker._apply_decision(application_row, candidate, decision, summary)
                    if claim_id is not None:
                        claim = service.store.get_claim(claim_id)
                        if claim is None or claim.status == "archived":
                            raise ValueError("retired claim cannot be applied")
                        # Atomic store receipt, never a before/after existence guess.
                        created = bool(outcome.get("created") or outcome.get("owned"))
                        pending["created_claim_ids" if created else "reused_claim_ids"] = [claim_id]
                        pending["citations"] = [[c.source, c.locator, c.excerpt] for c in claim.citations]
                    elif outcome.get("status") == "blocked":
                        raise ValueError("retired claim cannot be applied")
                    if decision.action.startswith("propose_"):
                        pending["proposal_ids"] = [e.id for e in service.list_events(claim_id=decision.target_claim_id, event_type="policy_decision", limit=100)
                                                   if decision.candidate_id in (e.payload_json or "")]
                    pending["completed"] = True
                    self._save(row)
                    self._hold_lease()
                    self.ledger.record_application("ao7-" + key, run_id=run_id, capture_id=capture["id"],
                                                   candidate_id=candidate.candidate_id, action=decision.action,
                                                   target_claim_id=decision.target_claim_id, created_claim_id=claim_id, now=self.now())
                    attempts += 1
            row["status"] = "applied"
            self._save(row)

    def _reconcile_intents(self, rows: list[dict[str, Any]], service) -> None:
        """Recover ownership after a crash between the two database commits."""
        for row in rows:
            changed = False
            for key, entry in row["metadata"].get("applications", {}).items():
                if entry.get("completed") or entry.get("created_claim_ids") or entry.get("reused_claim_ids"):
                    continue
                if entry["tenant_id"] != service.tenant_id:
                    raise ValueError("application intent tenant differs from authorized service")
                identity = hashlib.sha256(f"{entry['scope']}|{entry['candidate_id']}".encode()).hexdigest()[:24]
                claim = service.store.get_claim_by_idempotency_key(
                    "dream-" + identity, tenant_id=service.tenant_id, scope=entry["scope"], source_agent="dream-worker")
                if claim is not None:
                    owned = service.store.ingest_creation_receipt(claim.id).get("ingest_operation_id") == entry.get("operation_id", "ao7-" + key)
                    entry["created_claim_ids" if owned else "reused_claim_ids"] = [claim.id]
                    entry["citations"] = [[c.source, c.locator, c.excerpt] for c in claim.citations]
                    changed = True
            if changed:
                self._save(row)

    def _application_ordinal(self, rows: list[dict[str, Any]], key: str) -> int:
        keys = sorted(fingerprint([window["identity"], payload]) for row in rows
                      for window in row["metadata"].get("selection", {}).get("windows", [])
                      for payload in self.ledger.get_capture(window["capture_id"]).get("decisions") or [])
        return keys.index(key)


def rollback_backfill_cohort(runner: BackfillRunner, cohort_id: str, *, service=None,
                             dry_run: bool = True) -> dict[str, Any]:
    if dry_run or not (runner.config.state_dir / LEDGER_NAME).exists():
        return _rollback_backfill_cohort_locked(runner, cohort_id, service=service, dry_run=dry_run)
    if service is None:
        raise ValueError("explicit authorized service required for rollback")
    _require_sqlite_service(service)
    owner = uuid.uuid4().hex
    if not runner.ledger.acquire_lease("backfill-runner", owner, 3600, now=runner.now()):
        raise RuntimeError("backfill runner lease is busy; rollback not started")
    previous_owner, runner._lease_owner = runner._lease_owner, owner
    try:
        return _rollback_backfill_cohort_locked(runner, cohort_id, service=service, dry_run=False)
    finally:
        runner.ledger.release_lease("backfill-runner", owner)
        runner._lease_owner = previous_owner


def _rollback_backfill_cohort_locked(runner: BackfillRunner, cohort_id: str, *, service=None,
                                    dry_run: bool = True) -> dict[str, Any]:
    """Retire only exclusively owned claims, retaining manifest tombstones forever."""
    from memorymaster.core.lifecycle import transition_claim

    # Preview uses read-only ledger access and never constructs a service.
    path = runner.config.state_dir / LEDGER_NAME
    if not path.exists():
        return {"dry_run": dry_run, "created_claim_ids": [], "reused_claim_ids": [], "retired_claim_ids": []}
    with _read_ledger(path, dry_run=dry_run) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM backfill_inventory WHERE campaign_id=? AND cohort_id=?",
                                            (runner.config.campaign_id, cohort_id))]
    created: set[int] = set()
    reused: set[int] = set()
    retired: list[int] = []
    preserved: list[int] = []
    entries = []
    for row in rows:
        row["metadata"] = json.loads(row["metadata_json"])
        for key, entry in row["metadata"].get("applications", {}).items():
            entry.setdefault("operation_id", "ao7-" + key)
        entries.extend(row["metadata"].get("applications", {}).values())
        for entry in row["metadata"].get("applications", {}).values():
            created.update(entry.get("created_claim_ids", []))
            reused.update(entry.get("reused_claim_ids", []))
    if not dry_run:
        if service is None:
            raise ValueError("explicit authorized service required for rollback")
        runner._reconcile_intents(rows, service)
        entries = [entry for row in rows for entry in row["metadata"].get("applications", {}).values()]
        created = {claim_id for entry in entries for claim_id in entry.get("created_claim_ids", [])}
        reused = {claim_id for entry in entries for claim_id in entry.get("reused_claim_ids", [])}
        runner.pause(cohort_id=cohort_id, state="retired")
        for claim_id in sorted(created - reused):
            runner._hold_lease()
            entry = next(e for e in entries if claim_id in e.get("created_claim_ids", []))
            claims = service.list_claims(ids=[claim_id], scope_allowlist=[entry["scope"]], requesting_agent="dream-worker", limit=1)
            if not claims:
                preserved.append(claim_id)
                continue
            claim = claims[0]
            expected = entry.get("citations", [])
            citations = [[c.source, c.locator, c.excerpt] for c in claim.citations]
            if (claim.tenant_id != entry["tenant_id"] or claim.source_agent != "dream-worker"
                    or citations != expected or not expected):
                preserved.append(claim_id)
                continue
            # Independent validation/promotion or a later ingest defeats exclusive ownership.
            events = service.list_events(claim_id=claim_id, limit=10000)
            if len(events) >= 10000 or claim.status != "candidate" or any(e.event_type in {"validator", "deterministic_validator", "citation_added", "reinforcement"} or
                   (e.to_status == "confirmed" and e.from_status != "confirmed") for e in events):
                preserved.append(claim_id)
                continue
            guard = {"tenant_id": entry["tenant_id"], "scope": entry["scope"], "version": claim.version,
                     "citation_hash": fingerprint(citations), "last_event_id": max((event.id for event in events), default=0),
                     "operation_id": entry["operation_id"]}
            try:
                runner._hold_lease()
                transition_claim(service.store, claim_id, "archived", "ao7_rollback",
                                 event_payload={"ao7_retirement_guard": guard})
            except ConcurrentModificationError:
                preserved.append(claim_id)
                continue
            retired.append(claim_id)
        for row in rows:
            row["metadata"]["rollback"] = {"retired": retired, "preserved": preserved, "tombstone": True}
            row["status"] = "retired"
            runner._save(row)
    return {"dry_run": dry_run, "created_claim_ids": sorted(created), "reused_claim_ids": sorted(reused),
            "retired_claim_ids": retired, "preserved_claim_ids": preserved}
