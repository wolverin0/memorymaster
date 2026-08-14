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
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable


ACTIVE_JOB_STATES = ("pending", "leased", "retryable", "blocked")
TRUE_VALUES = {"1", "true", "yes", "on"}


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


def _connect_ro(db: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUE_VALUES


def check_runtime(config: ReviewConfig) -> ReviewResult:
    try:
        version = importlib.metadata.version("memorymaster")
    except importlib.metadata.PackageNotFoundError:
        return ReviewResult("runtime", Verdict.FAIL, "installed package unavailable")
    expected = config.expected_version or version
    verdict = Verdict.PASS if version == expected else Verdict.FAIL
    return ReviewResult("runtime", verdict, f"installed={version} expected={expected}")


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
    return ReviewResult(
        "feature_activation",
        Verdict.PASS if enabled == len(states) else Verdict.WARN,
        f"enabled={enabled}/{len(states)}",
        states,
    )


def check_graph_observations(config: ReviewConfig) -> ReviewResult:
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
            counts["observations"] = int(connection.execute(
                "SELECT COUNT(*) FROM graph_observations"
            ).fetchone()[0])
            support_row = connection.execute("""
                SELECT COUNT(*) edge_support_rows,
                       SUM(CASE WHEN cel.claim_id IS NULL OR e.id IS NULL OR s.id IS NULL
                                     OR e.sensitivity IS NULL OR s.sensitivity IS NULL
                                THEN 1 ELSE 0 END) unknown_sensitivity_rows
                FROM entity_edge_supports ees
                LEFT JOIN claim_evidence_links cel
                    ON cel.claim_id=ees.supporting_claim_id
                LEFT JOIN evidence_items e ON e.id=cel.evidence_item_id
                LEFT JOIN source_items s ON s.id=e.source_item_id
            """).fetchone()
            counts["edge_support_rows"] = int(support_row["edge_support_rows"] or 0)
            counts["unknown_sensitivity_rows"] = int(
                support_row["unknown_sensitivity_rows"] or 0
            )
            expired_leases = int(connection.execute(
                "SELECT COUNT(*) FROM graph_observation_jobs WHERE status='leased' "
                "AND lease_expires_at IS NOT NULL AND datetime(lease_expires_at)<=datetime('now')"
            ).fetchone()[0])
            counts["expired_leases"] = expired_leases
    except (OSError, sqlite3.Error) as exc:
        return ReviewResult("graph_observations", Verdict.FAIL, f"probe_error={type(exc).__name__}")
    if counts["blocked"] or expired_leases or counts["unknown_sensitivity_rows"]:
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
    except (OSError, sqlite3.Error) as exc:
        return ReviewResult("compiled_profile", Verdict.FAIL, f"probe_error={type(exc).__name__}")
    counts = {"completed_runs": completed, "active_facts": facts, "supports": supports, "mismatches": mismatches}
    if mismatches:
        verdict = Verdict.FAIL
    elif _enabled("MEMORYMASTER_COMPILED_PROFILE") and (completed == 0 or facts == 0):
        verdict = Verdict.WARN
    else:
        verdict = Verdict.PASS
    return ReviewResult("compiled_profile", verdict, "active facts must retain exact session support", counts)


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
    if not config.canary_query or not config.canary_human_id:
        return ReviewResult("retrieval_canary", Verdict.WARN, "canary not configured")
    try:
        ranking = retrieve(config.db, config.canary_query)
    except Exception as exc:  # noqa: BLE001 - review converts probe errors into evidence
        return ReviewResult("retrieval_canary", Verdict.FAIL, f"probe_error={type(exc).__name__}")
    rank = ranking.index(config.canary_human_id) + 1 if config.canary_human_id in ranking else 0
    return ReviewResult(
        "retrieval_canary",
        Verdict.PASS if rank else Verdict.FAIL,
        f"target={config.canary_human_id} rank={rank or 'missing'}",
        human_ids=tuple(ranking),
    )


def exit_code(results: Iterable[ReviewResult]) -> int:
    verdicts = {result.verdict for result in results}
    if Verdict.FAIL in verdicts:
        return 1
    if Verdict.WARN in verdicts:
        return 3
    return 0


def run_review(config: ReviewConfig) -> list[ReviewResult]:
    return [
        check_runtime(config),
        check_database(config),
        check_feature_activation(config),
        check_graph_observations(config),
        check_compiled_profile(config),
        check_recent_private_context(config),
        check_retrieval(config),
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--expected-version")
    parser.add_argument("--lookback-hours", type=int, default=8)
    parser.add_argument("--canary-query")
    parser.add_argument("--canary-human-id")
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
    )
    results = run_review(config)
    code = exit_code(results)
    payload = {
        "schema": "memorymaster.operational-review.v1",
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
