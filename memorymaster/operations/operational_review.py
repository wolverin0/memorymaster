"""Read-only operational review for a live personal MemoryMaster installation.

The review is deliberately independent of release timing. It records current
runtime evidence and never mutates claims, jobs, lifecycle state, or a success
watermark. Exit codes are 0=PASS, 1=FAIL, and 3=WARN.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable


ACTIVE_JOB_STATES = ("pending", "leased", "retryable", "blocked")
TRUE_VALUES = {"1", "true", "yes", "on"}
PROFILE_MAX_SUPPORT_AGE_DAYS = 7
# Jev decisions (4.9.0): a live hook surface (every prompt / session) must log within
# a day or the review FAILs; a live batch surface (steward cycle, Dreaming) WARNs after
# a day and a half; route is exempt while recall is not off (the prompt hook skips S7
# by design) and otherwise WARNs after a day, like skills (on demand).  Spend stays
# under the cap; more than 30 % fallbacks over at least 20 decisions that asked Jev
# (``skip:`` rows are volume, not failures) is a broken surface; a weekly ECE rise
# above 0.05 (with at least 20 matured items per week) is drift.
JEV_SILENCE_HOURS = 24
JEV_BATCH_SILENCE_HOURS = 36
JEV_HOOK_SURFACES = ("recall", "session", "hints")
JEV_BATCH_SURFACES = ("revalidate", "dedup", "ingest")
JEV_FALLBACK_SHARE = 0.30
JEV_FALLBACK_MIN_DECISIONS = 20
JEV_ECE_RISE = 0.05
JEV_ECE_MIN_ITEMS = 20
# Daily feature-review checkpoint (review F-08): delivered at least every 26 h.
CHECKPOINT_LOG_ENV = "MEMORYMASTER_CHECKPOINT_LOG"
CHECKPOINT_MAX_AGE_HOURS = 26
CHECKPOINT_TAIL_BYTES = 256 * 1024
_CHECKPOINT_OK = re.compile(r"^(\S+)\s+(orca-poke|poke-pane)\s+OK\b")
_CHECKPOINT_FAIL = re.compile(r"^\S+\s+(orca-poke|poke-pane)\s+FAIL\b")


class Verdict(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class ReviewResult:
    name: str
    verdict: Verdict
    detail: str
    counts: dict[str, int] | None = None
    human_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewConfig:
    db: Path
    expected_version: str | None = None
    lookback_hours: int = 8
    canary_query: str | None = None
    canary_human_id: str | None = None
    # Several (query, human_id) canaries (review F-12): one boundary canary at
    # rank 5 of 5 is weak evidence. The single fields above still work and
    # are probed first; duplicates are probed once.
    canaries: tuple[tuple[str, str], ...] = ()
    # Decisions ledger (default: MEMORYMASTER_DECISIONS_DB) and checkpoint log
    # (default: MEMORYMASTER_CHECKPOINT_LOG, then ~/.memorymaster/checkpoints/).
    decisions_db: Path | None = None
    checkpoint_log: Path | None = None


def _configured_canaries(config: ReviewConfig) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if config.canary_query and config.canary_human_id:
        pairs.append((config.canary_query, config.canary_human_id))
    for query, human_id in config.canaries:
        if query and human_id and (query, human_id) not in pairs:
            pairs.append((query, human_id))
    return pairs


def _connect_ro(db: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUE_VALUES


def _workspace_version(db: Path) -> str | None:
    """Version declarada en el pyproject.toml del checkout que contiene la base.

    Devuelve None si no hay pyproject alcanzable: no poder leerlo es "no se"
    y no debe convertirse en un FAIL, que es lo que arruinaria el check en una
    instalacion desde wheel.
    """
    for parent in [db.resolve().parent, *db.resolve().parents]:
        candidate = parent / "pyproject.toml"
        if not candidate.exists():
            continue
        try:
            for line in candidate.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("version") and "=" in stripped:
                    return stripped.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            return None
        return None
    return None


def check_runtime(config: ReviewConfig) -> ReviewResult:
    """Compara el paquete INSTALADO contra la version del checkout.

    POR QUE NO CONTRA UN CONFIG A MANO. `expected_version` vivia en un archivo
    fuera del repo (AppData) que habia que actualizar en cada release, y eso
    fallo DOS VECES: 31 corridas seguidas en FAIL con 4.7.6 contra 4.8.4
    (2026-08-20), y de nuevo el 2026-08-29 con 4.8.4 contra 4.8.5. Bumpear la
    version y actualizar el config eran dos actos unidos solo por la memoria de
    alguien, y ningun test del repo podia enforzarlo porque el archivo no existe
    en CI.

    Leer el pyproject del workspace mueve la referencia SOLA con cada release y
    ademas detecta la falla que de verdad importa en una instalacion editable:
    bumpeaste la version y no reinstalaste, o sea que el paquete que corre no es
    el codigo que commiteaste.

    El config explicito sigue teniendo prioridad, para no romper a quien lo use
    a proposito. Si no hay ninguno de los dos, no hay nada que comparar y se
    reporta PASS diciendolo, en vez de inventar una expectativa.
    """
    try:
        version = importlib.metadata.version("memorymaster")
    except importlib.metadata.PackageNotFoundError:
        return ReviewResult("runtime", Verdict.FAIL, "installed package unavailable")

    expected = config.expected_version or _workspace_version(config.db)
    if expected is None:
        return ReviewResult(
            "runtime", Verdict.PASS, f"installed={version} expected=(sin referencia)"
        )
    origen = "config" if config.expected_version else "pyproject"
    verdict = Verdict.PASS if version == expected else Verdict.FAIL
    return ReviewResult(
        "runtime", verdict, f"installed={version} expected={expected} ({origen})"
    )


def check_database(config: ReviewConfig) -> ReviewResult:
    try:
        with _connect_ro(config.db) as connection:
            quick = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
            foreign_keys = sum(1 for _ in connection.execute("PRAGMA foreign_key_check"))
            migration = int(connection.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0] or 0)
    except (OSError, sqlite3.Error) as exc:
        return ReviewResult("database", Verdict.FAIL, f"probe_error={type(exc).__name__}")
    ok = quick == ["ok"] and foreign_keys == 0 and migration >= 21
    return ReviewResult(
        "database",
        Verdict.PASS if ok else Verdict.FAIL,
        f"quick_check={','.join(quick[:3])} foreign_key_errors={foreign_keys} migration={migration}",
    )


def check_feature_activation(_config: ReviewConfig) -> ReviewResult:
    states = {
        "graph_observations": int(_enabled("MEMORYMASTER_GRAPH_OBSERVATIONS")),
        "compiled_profile": int(_enabled("MEMORYMASTER_COMPILED_PROFILE")),
    }
    enabled = sum(states.values())
    # These flags belong to this process, not necessarily the scheduled worker.
    detalle = (
        f"review-process enabled={enabled}/{len(states)}; "
        "worker activation unverified"
    )
    return ReviewResult("feature_activation", Verdict.PASS, detalle, states)


def _count_ineligible_confirmed_observations(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            """
                SELECT COUNT(*) FROM (
                    SELECT go.observation_claim_id
                    FROM graph_observations go
                    JOIN claims observation
                        ON observation.id=go.observation_claim_id
                    LEFT JOIN graph_observation_supports support
                        ON support.observation_claim_id=go.observation_claim_id
                    LEFT JOIN claims supporting
                        ON supporting.id=support.supporting_claim_id
                    LEFT JOIN evidence_items evidence
                        ON evidence.id=support.evidence_item_id
                    LEFT JOIN source_items source
                        ON source.id=support.source_item_id
                    WHERE observation.status='confirmed'
                    GROUP BY go.observation_claim_id
                    HAVING COUNT(DISTINCT support.supporting_claim_id) < 3
                        OR COUNT(DISTINCT support.evidence_item_id) < 2
                        OR COUNT(DISTINCT support.source_item_id) < 2
                        OR MIN(CASE
                            WHEN supporting.status='confirmed'
                             AND supporting.claim_type!='observation'
                             AND supporting.scope=observation.scope
                             AND COALESCE(supporting.tenant_id, '')=
                                 COALESCE(observation.tenant_id, '')
                             AND supporting.confidence>=0.65
                             AND evidence.sensitivity='none'
                             AND source.sensitivity='none'
                             AND source.retired_at IS NULL
                            THEN 1 ELSE 0 END)=0
                )
                """
        ).fetchone()[0]
    )


def _graph_support_counts(connection: sqlite3.Connection) -> dict[str, int]:
    row = connection.execute("""
        SELECT COUNT(*) edge_support_rows,
               SUM(CASE WHEN cel.claim_id IS NULL OR e.id IS NULL OR s.id IS NULL
                             OR e.sensitivity IS NULL OR s.sensitivity IS NULL
                        THEN 1 ELSE 0 END) unknown_sensitivity_rows
        FROM entity_edge_supports ees
        LEFT JOIN claim_evidence_links cel ON cel.claim_id=ees.supporting_claim_id
        LEFT JOIN evidence_items e ON e.id=cel.evidence_item_id
        LEFT JOIN source_items s ON s.id=e.source_item_id
    """).fetchone()
    return {
        "edge_support_rows": int(row["edge_support_rows"] or 0),
        "unknown_sensitivity_rows": int(row["unknown_sensitivity_rows"] or 0),
        "ineligible_confirmed_observations": (
            _count_ineligible_confirmed_observations(connection)
        ),
    }


def _discovery_outcome_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Split completed discovery jobs by what they concluded.

    ``completed_discovery`` on its own says the machine ran. Production showed
    3,146 of them against 2 observations, and nothing in this report said so.
    """
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(graph_observation_jobs)")
    }
    if "outcome" not in columns:
        return {"discovery_outcomes_recorded": 0}
    rows = connection.execute(
        """SELECT COALESCE(outcome, 'unrecorded') outcome, COUNT(*) count
           FROM graph_observation_jobs
           WHERE stage='discover' AND status='completed' GROUP BY outcome"""
    ).fetchall()
    counts = {f"discovery_{str(row['outcome'])}": int(row["count"]) for row in rows}
    counts["discovery_outcomes_recorded"] = sum(
        value for key, value in counts.items() if key != "discovery_unrecorded"
    )
    return counts


def _retained_graph_state(config: ReviewConfig) -> ReviewResult:
    """A review-process flag does not establish the scheduled worker's environment."""
    try:
        with _connect_ro(config.db) as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM graph_observation_jobs GROUP BY status"
            ).fetchall()
            counts = {state: 0 for state in ACTIVE_JOB_STATES}
            counts.update({str(row[0]): int(row[1]) for row in rows})
            counts["review_process_enabled"] = 0
            counts["expired_leases"] = int(connection.execute(
                "SELECT COUNT(*) FROM graph_observation_jobs WHERE status='leased' "
                "AND lease_expires_at IS NOT NULL "
                "AND datetime(lease_expires_at)<=datetime('now')"
            ).fetchone()[0])
    except (OSError, sqlite3.Error) as exc:
        return ReviewResult("graph_observations", Verdict.FAIL, f"probe_error={type(exc).__name__}")
    attention = (
        counts["blocked"] or counts["retryable"] or counts["expired_leases"]
        or counts["pending"] > 100
    )
    return ReviewResult(
        "graph_observations", Verdict.WARN if attention else Verdict.PASS,
        "review process flag is off; worker activation unverified; retained queue inspected",
        counts,
    )


def check_graph_observations(config: ReviewConfig) -> ReviewResult:
    if not _enabled("MEMORYMASTER_GRAPH_OBSERVATIONS"):
        return _retained_graph_state(config)
    marks = ",".join("?" for _ in ACTIVE_JOB_STATES)
    try:
        with _connect_ro(config.db) as connection:
            rows = connection.execute(
                f"SELECT status, COUNT(*) count FROM graph_observation_jobs "
                f"WHERE status IN ({marks}) GROUP BY status",
                ACTIVE_JOB_STATES,
            ).fetchall()
            counts = {state: 0 for state in ACTIVE_JOB_STATES}
            counts.update({str(row["status"]): int(row["count"]) for row in rows})
            counts["completed_discovery"] = int(connection.execute(
                "SELECT COUNT(*) FROM graph_observation_jobs WHERE stage='discover' AND status='completed'"
            ).fetchone()[0])
            counts.update(_discovery_outcome_counts(connection))
            counts["observations"] = int(connection.execute(
                "SELECT COUNT(*) FROM graph_observations"
            ).fetchone()[0])
            counts.update(_graph_support_counts(connection))
            expired_leases = int(connection.execute(
                "SELECT COUNT(*) FROM graph_observation_jobs WHERE status='leased' "
                "AND lease_expires_at IS NOT NULL AND datetime(lease_expires_at)<=datetime('now')"
            ).fetchone()[0])
            counts["expired_leases"] = expired_leases
    except (OSError, sqlite3.Error) as exc:
        return ReviewResult("graph_observations", Verdict.FAIL, f"probe_error={type(exc).__name__}")
    if (
        counts["blocked"]
        or expired_leases
        or counts["unknown_sensitivity_rows"]
        or counts["ineligible_confirmed_observations"]
    ):
        verdict = Verdict.FAIL
    elif counts["retryable"] or counts["pending"] > 100:
        verdict = Verdict.WARN
    else:
        verdict = Verdict.PASS
    return ReviewResult(
        "graph_observations",
        verdict,
        "every graph support requires explicit source and evidence sensitivity",
        counts,
    )


def check_compiled_profile(config: ReviewConfig) -> ReviewResult:
    try:
        with _connect_ro(config.db) as connection:
            completed = int(connection.execute(
                "SELECT COUNT(*) FROM compiled_profile_runs WHERE status='completed'"
            ).fetchone()[0])
            facts = int(connection.execute(
                "SELECT COUNT(*) FROM compiled_profile_facts WHERE status='active'"
            ).fetchone()[0])
            supports = int(connection.execute("SELECT COUNT(*) FROM compiled_profile_supports").fetchone()[0])
            mismatches = int(connection.execute("""
                SELECT COUNT(*) FROM compiled_profile_facts f
                LEFT JOIN (
                    SELECT fact_id, COUNT(*) support_count, COUNT(DISTINCT session_id) session_count
                    FROM compiled_profile_supports GROUP BY fact_id
                ) s ON s.fact_id=f.id
                WHERE f.status='active' AND (
                    f.support_count<>COALESCE(s.support_count,0)
                    OR f.independent_sessions<>COALESCE(s.session_count,0)
                )
            """).fetchone()[0])
            # Freshness (review F-03): the injected profile froze for weeks while
            # this check stayed PASS, because it verified manifest integrity only.
            newest_age = connection.execute("""
                SELECT julianday('now') - MAX(julianday(s.supported_at))
                FROM compiled_profile_supports s
                JOIN compiled_profile_facts f ON f.id = s.fact_id
                WHERE f.status = 'active'
            """).fetchone()[0]
    except (OSError, sqlite3.Error) as exc:
        return ReviewResult("compiled_profile", Verdict.FAIL, f"probe_error={type(exc).__name__}")
    counts = {"completed_runs": completed, "active_facts": facts, "supports": supports, "mismatches": mismatches}
    detail = "active facts must retain exact session support"
    stale = False
    if facts:
        # Real (float) age: truncating to whole days let 7.9 pass a 7-day limit.
        # Clock skew under a day (a support stamped slightly ahead) is fresh;
        # a day or more ahead is an unknown age.
        age = None if newest_age is None or newest_age <= -1 else max(0.0, float(newest_age))
        age_days = round(age, 1) if age is not None else -1
        counts["newest_support_age_days"] = age_days
        stale = age is None or age > PROFILE_MAX_SUPPORT_AGE_DAYS
        if stale:
            if age is None:
                shown = "unknown"
            elif age_days > PROFILE_MAX_SUPPORT_AGE_DAYS:
                shown = f"{age_days:g}d"
            else:  # 7.04 rounds to 7.0; do not print "7d old (max 7d)".
                shown = f"more than {PROFILE_MAX_SUPPORT_AGE_DAYS}d"
            detail += (
                f"; newest active-fact support is {shown} old "
                f"(max {PROFILE_MAX_SUPPORT_AGE_DAYS}d): the injected profile is not being refreshed"
            )
    if mismatches:
        verdict = Verdict.FAIL
    elif stale or (_enabled("MEMORYMASTER_COMPILED_PROFILE") and (completed == 0 or facts == 0)):
        verdict = Verdict.WARN
    else:
        verdict = Verdict.PASS
    return ReviewResult("compiled_profile", verdict, detail, counts)


def check_recent_private_context(config: ReviewConfig) -> ReviewResult:
    from memorymaster.core.security import _CLAIM_ONLY_PATTERNS

    since = datetime.now(timezone.utc) - timedelta(hours=config.lookback_hours)
    matches: list[str] = []
    scanned = 0
    try:
        with _connect_ro(config.db) as connection:
            rows = connection.execute(
                "SELECT human_id, text, subject, predicate, object_value FROM claims "
                "WHERE datetime(created_at)>=datetime(?)",
                (since.isoformat(),),
            )
            for row in rows:
                scanned += 1
                content = "\n".join(str(row[key] or "") for key in ("text", "subject", "predicate", "object_value"))
                if any(pattern.search(content) for _, pattern in _CLAIM_ONLY_PATTERNS):
                    matches.append(str(row["human_id"] or ""))
    except (OSError, sqlite3.Error) as exc:
        return ReviewResult("recent_private_context", Verdict.FAIL, f"probe_error={type(exc).__name__}")
    counts = {"lookback_hours": config.lookback_hours, "claims_scanned": scanned, "matches": len(matches)}
    return ReviewResult(
        "recent_private_context",
        Verdict.FAIL if matches else Verdict.PASS,
        "claim fields only; raw source/evidence is intentionally outside this check",
        counts,
        tuple(matches[:10]),
    )


def _default_retrieval(db: Path, query: str) -> list[str]:
    from memorymaster.core.service import MemoryService
    from memorymaster.recall.planner import RetrievalRequest, build_retrieval_plan

    service = MemoryService(str(db), workspace_root=db.parent, read_only=True)
    plan = build_retrieval_plan(RetrievalRequest(query_text=query, limit=5, trust_mode="trusted"))
    rows = service.query_rows(
        query_text=plan.search_text,
        limit=plan.limit,
        include_stale=False,
        include_conflicted=False,
        include_candidates=False,
        retrieval_mode=plan.effective_mode,
        allow_sensitive=False,
        scope_allowlist=None,
        record_accesses=False,
    )
    return [str(row["claim"].human_id or "") for row in rows]


def check_retrieval(
    config: ReviewConfig,
    *,
    retrieve: Callable[[Path, str], list[str]] = _default_retrieval,
) -> ReviewResult:
    canaries = _configured_canaries(config)
    if not canaries:
        return ReviewResult("retrieval_canary", Verdict.WARN, "canary not configured")
    ranks: list[tuple[str, int]] = []
    rankings: list[list[str]] = []
    for query, human_id in canaries:
        try:
            ranking = retrieve(config.db, query)
        except Exception as exc:  # noqa: BLE001 - review converts probe errors into evidence
            return ReviewResult("retrieval_canary", Verdict.FAIL, f"probe_error={type(exc).__name__}")
        rankings.append(ranking)
        ranks.append((human_id, ranking.index(human_id) + 1 if human_id in ranking else 0))
    detail = "; ".join(f"target={human_id} rank={rank or 'missing'}" for human_id, rank in ranks)
    verdict = Verdict.PASS if all(rank for _, rank in ranks) else Verdict.FAIL
    if len(canaries) == 1:
        return ReviewResult("retrieval_canary", verdict, detail, human_ids=tuple(rankings[0]))
    counts = {"canaries": len(canaries), "found": sum(1 for _, rank in ranks if rank)}
    for human_id, rank in ranks:
        key = f"rank:{human_id}"
        # A human id probed by two queries reports its worst rank (0 = missing).
        counts[key] = rank if key not in counts else (0 if 0 in (rank, counts[key]) else max(rank, counts[key]))
    return ReviewResult("retrieval_canary", verdict, detail, counts, tuple(human_id for human_id, _ in ranks))


def _utc(moment: datetime | None) -> datetime:
    return (moment or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _jev_ece_rises(ledger, end: datetime) -> list[str]:
    """Compare the two most recent matured weeks per outcome maturity.

    ``compute_metrics`` only counts items decided at least one maturity before its
    ``until``: a week ending now holds no matured item for a 7-day lifecycle outcome
    (``ingest.usefulness`` -> ``steward_confirmed``), so each maturity class is
    compared on the weeks ending ``maturity`` before now.  All the windows come from
    one ``calibration_windows`` read of the calibrated questions.
    """
    from memorymaster.decisions.metrics import DEFAULT_MATURITY, LIFECYCLE_MATURITY, calibration_windows

    week = timedelta(days=7)
    maturities = sorted({*DEFAULT_MATURITY.values(), LIFECYCLE_MATURITY})
    reports = calibration_windows(ledger, [window for maturity in maturities for window in (
        (end - maturity - week, end), (end - maturity - 2 * week, end - week))])
    current: dict = {}
    previous: dict = {}
    for index, maturity in enumerate(maturities):
        def matures(entry: dict, maturity: timedelta = maturity) -> bool:
            return DEFAULT_MATURITY.get(entry.get("positive_kind"), LIFECYCLE_MATURITY) == maturity

        now_week, week_before = reports[2 * index], reports[2 * index + 1]
        current.update({key: entry for key, entry in now_week.items() if matures(entry)})
        previous.update({key: entry for key, entry in week_before.items() if matures(entry)})
    rises = []
    for key in sorted(set(current) & set(previous)):
        now_c, before = current[key], previous[key]
        if min(now_c["n"], before["n"]) < JEV_ECE_MIN_ITEMS or now_c["ece"] is None or before["ece"] is None:
            continue
        if now_c["ece"] - before["ece"] > JEV_ECE_RISE:
            rises.append(f"{key} ece {before['ece']:.3f}->{now_c['ece']:.3f}")
    return rises


def _jev_silence(live: list[str], day: dict, batch_window: dict, recall_mode: str
                 ) -> tuple[list[str], list[str], list[str]]:
    """Silent live surfaces: (hooks -> FAIL, batch surfaces over 36 h -> WARN, route/skills -> WARN)."""
    hooks = [s for s in live if s in JEV_HOOK_SURFACES and not day.get(s)]
    batches = [s for s in live if s in JEV_BATCH_SURFACES and not batch_window.get(s)]
    others = [s for s in live if s not in JEV_HOOK_SURFACES and s not in JEV_BATCH_SURFACES and not day.get(s)
              and not (s == "route" and recall_mode != "off")]
    return hooks, batches, others


def _silence_findings(hooks: list[str], batches: list[str], others: list[str]) -> tuple[list[str], list[str]]:
    failing = [f"silent_{JEV_SILENCE_HOURS}h={','.join(hooks)}"] if hooks else []
    warning = [f"silent_{hours}h={','.join(names)}"
               for hours, names in ((JEV_BATCH_SILENCE_HOURS, batches), (JEV_SILENCE_HOURS, others)) if names]
    return failing, warning


def check_jev_decisions(config: ReviewConfig, *, now: datetime | None = None) -> ReviewResult:
    """Health of live Jev decisions from the decisions ledger alone, opened read-only."""
    from memorymaster.decisions.config import SURFACES, DecisionConfig
    from memorymaster.decisions.ledger import utc_iso
    from memorymaster.surfaces.jev_review import ReadOnlyLedger

    decision_config = DecisionConfig.from_env()
    ledger = ReadOnlyLedger(config.decisions_db or decision_config.decisions_db)
    end = _utc(now)
    live = [surface for surface in SURFACES if decision_config.mode_for(surface) == "live"]
    recall_mode = decision_config.mode_for("recall")
    if not ledger.exists():
        if not live:
            return ReviewResult("jev_decisions", Verdict.PASS, "Jev off: no decisions ledger", {"live_surfaces": 0})
        failing, warning = _silence_findings(*_jev_silence(live, {}, {}, recall_mode))
        verdict = Verdict.FAIL if failing else Verdict.WARN if warning else Verdict.PASS
        return ReviewResult("jev_decisions", verdict, f"live={','.join(live)} but no decisions ledger exists",
                            {"live_surfaces": len(live)})
    day_start = end.replace(hour=0, minute=0, second=0, microsecond=0)
    day = utc_iso(end - timedelta(hours=JEV_SILENCE_HOURS))
    asked = "NOT COALESCE(fallback_reason GLOB 'skip:*', 0)"  # a skip row did not ask Jev
    try:
        recent = ledger.query(
            "SELECT surface, SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS n, "
            f"SUM(CASE WHEN ts >= ? AND mode != 'off' AND {asked} THEN 1 ELSE 0 END) AS active, "
            f"SUM(CASE WHEN ts >= ? AND mode != 'off' AND fallback_reason IS NOT NULL AND {asked} THEN 1 ELSE 0 END) "
            "AS fallbacks, COUNT(*) AS n_batch_window FROM decisions WHERE ts >= ? AND ts <= ? GROUP BY surface",
            (day, day, day, utc_iso(end - timedelta(hours=JEV_BATCH_SILENCE_HOURS)), utc_iso(end)),
        )
        spent = ledger.query("SELECT COALESCE(SUM(cost_usd), 0.0) AS spend FROM decisions WHERE ts >= ? AND ts <= ?",
                             (utc_iso(day_start), utc_iso(end)))
        # An intent younger than a minute is a request still in flight, not an orphan.
        orphans = _jev_orphan_intents(ledger, utc_iso(day_start), day, utc_iso(end - timedelta(seconds=60)))
        rises = _jev_ece_rises(ledger, end)
    except Exception as exc:  # noqa: BLE001 - LedgerReadError, sqlite3/OS errors or anything unexpected:
        # run_review has no per-check guard, so this is one FAIL row instead of a lost review.
        return ReviewResult("jev_decisions", Verdict.FAIL, f"probe_error={type(exc).__name__}")
    by_surface = {str(row["surface"]): row for row in recent}
    # A request whose decision row never landed still cost money: its send intent counts.
    cost_today = (float(spent[0]["spend"]) if spent else 0.0) + orphans["cost_today"]
    cap = decision_config.daily_usd_cap
    hooks, batches, others = _jev_silence(live, {s: int(r["n"] or 0) for s, r in by_surface.items()},
                                          {s: int(r["n_batch_window"] or 0) for s, r in by_surface.items()},
                                          recall_mode)
    failing, warning = _silence_findings(hooks, batches, others)
    broken = []
    for surface, row in sorted(by_surface.items()):
        active, fallbacks = int(row["active"] or 0), int(row["fallbacks"] or 0)
        if active >= JEV_FALLBACK_MIN_DECISIONS and fallbacks / active > JEV_FALLBACK_SHARE:
            broken.append(f"{surface} {fallbacks}/{active}")
    problems = list(failing)
    if cost_today > cap:
        problems.append(f"cost_today=${cost_today:.4f} over cap ${cap:.2f}")
    if broken:
        problems.append(f"fallback_24h>{JEV_FALLBACK_SHARE:.0%}: {'; '.join(broken)}")
    warnings = list(warning)
    if rises:
        warnings.append(f"weekly calibration worsened: {'; '.join(rises)}")
    if orphans["n_24h"]:
        warnings.append(f"orphan_send_intents_24h={orphans['n_24h']} (requests sent without a decision row)")
    counts = {"live_surfaces": len(live), "decisions_24h": sum(int(r["n"] or 0) for r in recent),
              "silent_live_surfaces": len(hooks), "quiet_live_surfaces": len(batches) + len(others),
              "high_fallback_surfaces": len(broken), "ece_rises": len(rises),
              "orphan_send_intents_24h": orphans["n_24h"]}
    summary = f"live={','.join(live) or 'none'}; cost_today=${cost_today:.4f}/cap ${cap:.2f}"
    if problems:
        return ReviewResult("jev_decisions", Verdict.FAIL, "; ".join(problems + warnings) + f" ({summary})", counts)
    if warnings:
        return ReviewResult("jev_decisions", Verdict.WARN, "; ".join(warnings) + f" ({summary})", counts)
    return ReviewResult("jev_decisions", Verdict.PASS, summary, counts)


def _jev_orphan_intents(ledger: Any, day_start: str, since_24h: str, until: str) -> dict[str, Any]:
    """Send intents with no decision row: count in the last 24 h and cost since midnight UTC."""
    if not ledger.query("SELECT 1 AS present FROM sqlite_master WHERE type = 'table' AND name = 'send_intents'"):
        return {"n_24h": 0, "cost_today": 0.0}
    orphan = "FROM send_intents i WHERE NOT EXISTS (SELECT 1 FROM decisions d WHERE d.decision_id = i.decision_id)"
    rows = ledger.query(
        f"SELECT COALESCE(SUM(CASE WHEN i.ts >= ? AND i.ts <= ? THEN 1 ELSE 0 END), 0) AS n_24h, "
        f"COALESCE(SUM(CASE WHEN i.ts >= ? AND i.ts <= ? THEN i.est_cost_usd ELSE 0.0 END), 0.0) AS cost_today "
        f"{orphan}", (since_24h, until, day_start, until))
    return {"n_24h": int(rows[0]["n_24h"] or 0), "cost_today": float(rows[0]["cost_today"] or 0.0)}


def _checkpoint_log_path(config: ReviewConfig) -> Path:
    if config.checkpoint_log is not None:
        return config.checkpoint_log
    configured = os.environ.get(CHECKPOINT_LOG_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".memorymaster" / "checkpoints" / "feature-checkpoint.log"


def check_checkpoint_delivery(config: ReviewConfig, *, now: datetime | None = None) -> ReviewResult:
    """WARN when the daily checkpoint was last delivered more than 26 h ago (review F-08)."""
    path = _checkpoint_log_path(config)
    end = _utc(now)
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - CHECKPOINT_TAIL_BYTES))
            text = handle.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ReviewResult("checkpoint_delivery", Verdict.WARN, "checkpoint log not found: delivery unverified")
    except OSError as exc:
        return ReviewResult("checkpoint_delivery", Verdict.WARN, f"probe_error={type(exc).__name__}")
    last: tuple[datetime, str] | None = None
    ok_lines = fail_lines = 0
    for raw in text.splitlines():
        line = raw.strip()
        match = _CHECKPOINT_OK.match(line)
        if match is None:
            fail_lines += bool(_CHECKPOINT_FAIL.match(line))
            continue
        stamp = _parse_utc(match.group(1))
        if stamp is None:
            continue
        ok_lines += 1
        if last is None or stamp > last[0]:
            last = (stamp, match.group(2))
    counts = {"ok_lines": ok_lines, "fail_lines": fail_lines}
    if last is None:
        return ReviewResult("checkpoint_delivery", Verdict.WARN,
                            f"no successful delivery in the log tail ({fail_lines} failures)", counts)
    age_hours = max(0.0, (end - last[0]).total_seconds() / 3600)
    counts["last_delivery_age_hours"] = int(age_hours)
    detail = (f"last successful delivery {last[0].isoformat()} via {last[1]}, "
              f"{age_hours:.1f}h ago (max {CHECKPOINT_MAX_AGE_HOURS}h)")
    verdict = Verdict.WARN if age_hours > CHECKPOINT_MAX_AGE_HOURS else Verdict.PASS
    return ReviewResult("checkpoint_delivery", verdict, detail, counts)


REVIEW_CHECKS = (check_runtime, check_database, check_feature_activation, check_graph_observations,
                 check_compiled_profile, check_recent_private_context, check_retrieval,
                 check_jev_decisions, check_checkpoint_delivery)


def exit_code(results: Iterable[ReviewResult]) -> int:
    verdicts = {result.verdict for result in results}
    if Verdict.FAIL in verdicts:
        return 1
    if Verdict.WARN in verdicts:
        return 3
    return 0


def run_review(config: ReviewConfig) -> list[ReviewResult]:
    from memorymaster.operations.review_attempt import phase_progress

    results = []
    for check in REVIEW_CHECKS:
        phase_progress(check.__name__)
        started = time.monotonic()
        try:
            results.append(check(config))
        finally:
            phase_progress(check.__name__, time.monotonic() - started)
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--expected-version")
    parser.add_argument("--lookback-hours", type=int, default=8)
    parser.add_argument("--canary-query")
    parser.add_argument("--canary-human-id")
    parser.add_argument(
        "--canary", nargs=2, action="append", metavar=("QUERY", "HUMAN_ID"), default=[],
        help="additional retrieval canary; repeat for several",
    )
    parser.add_argument("--decisions-db", help="Jev decisions ledger (default: MEMORYMASTER_DECISIONS_DB)")
    parser.add_argument("--checkpoint-log", help="feature checkpoint log (default: MEMORYMASTER_CHECKPOINT_LOG)")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = ReviewConfig(
        db=Path(args.db).expanduser().resolve(),
        expected_version=args.expected_version,
        lookback_hours=max(1, min(168, args.lookback_hours)),
        canary_query=args.canary_query,
        canary_human_id=args.canary_human_id,
        canaries=tuple((query, human_id) for query, human_id in args.canary),
        decisions_db=Path(args.decisions_db).expanduser().resolve() if args.decisions_db else None,
        checkpoint_log=Path(args.checkpoint_log).expanduser().resolve() if args.checkpoint_log else None,
    )
    results = run_review(config)
    code = exit_code(results)
    payload = {
        "schema": "memorymaster.operational-review.v1",
        "attempt_id": os.environ.get("MEMORYMASTER_REVIEW_ATTEMPT_ID"),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "review_performed": True,
        "database_mutations": 0,
        "verdict": {0: "PASS", 1: "FAIL", 3: "WARN"}[code],
        "exit_code": code,
        "checks": [{**asdict(item), "verdict": item.verdict.value} for item in results],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for item in results:
            print(f"{item.name}: {item.verdict.value} - {item.detail}")
        print(f"overall: {payload['verdict']} (exit {code})")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
