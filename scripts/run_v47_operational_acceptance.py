"""Fail-closed v4.7 operational acceptance gate.

Exit codes intentionally match the fleet steward gate: 0 accepted, 1 failed,
3 incomplete because a scheduled observation is not yet due.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
try:
    sys.path.remove(str(REPO_ROOT))
except ValueError:
    pass
sys.path.insert(0, str(REPO_ROOT))


EXPECTED_VERSION = "4.7.0"
TARGET_HUMAN_ID = "mm-8aef"
TARGET_QUERY = "why does wezterm cli time out from Node but not from bash"
ACTIVE_OBSERVATION_STATES = ("pending", "leased", "retryable", "blocked")
TASK_NAMES = (
    "MemoryMaster-Dreaming",
    "MemoryMasterSteward",
    "MemoryMaster-MCP-HTTP-Hermes",
    "MemoryMaster-Checkpoint-Daily",
    "MemoryMaster-Checkpoint-Weekly",
)
CHECKPOINT_TASKS = (
    "MemoryMaster-Checkpoint-Daily",
    "MemoryMaster-Checkpoint-Weekly",
)


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_YET_DUE = "NOT-YET-DUE"


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    verdict: Verdict
    detail: str
    due_at: str | None = None


@dataclass(frozen=True, slots=True)
class GateConfig:
    db: Path
    runtime_python: Path
    base_url: str
    receipt_file: Path
    session_hook: Path
    expected_version: str = EXPECTED_VERSION
    retrieval_samples: int = 5
    retrieval_p95_seconds: float = 2.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def _connect_ro(db: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _discover_db(explicit: str | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env_db = os.environ.get("MEMORYMASTER_DEFAULT_DB")
    if env_db:
        candidates.append(Path(env_db).expanduser())
    candidates.append(Path.cwd() / "memorymaster.db")
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
        candidates.append(Path(common).resolve().parent / "memorymaster.db")
    except (OSError, subprocess.SubprocessError):
        pass
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return candidates[0].resolve()


def _python_version(executable: Path) -> str | None:
    try:
        run = subprocess.run(
            [str(executable), "-c", "import memorymaster; print(memorymaster.__version__)"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except OSError:
        return None
    return run.stdout.strip() if run.returncode == 0 else None


def _discover_runtime_python(explicit: str | None, expected: str) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_python = os.environ.get("MEMORYMASTER_RUNTIME_PYTHON")
    if env_python:
        return Path(env_python).expanduser().resolve()
    root = Path.home() / ".memorymaster" / "runtime"
    suffix = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    candidates = sorted(root.glob(f"*/{suffix.as_posix()}"), key=lambda p: p.stat().st_mtime, reverse=True)
    return next((item for item in candidates if _python_version(item) == expected), Path(sys.executable))


def check_identity_and_db(config: GateConfig) -> CheckResult:
    try:
        import memorymaster

        source_version = memorymaster.__version__
        runtime_version = _python_version(config.runtime_python)
        with _connect_ro(config.db) as connection:
            migration = connection.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0]
            quick = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
    except (OSError, sqlite3.Error) as exc:
        return CheckResult("condition_1_identity_db", Verdict.FAIL, f"probe_error={type(exc).__name__}")
    ok = (
        source_version == config.expected_version
        and runtime_version == config.expected_version
        and int(migration or 0) >= 21
        and quick == ["ok"]
    )
    detail = (
        f"source={source_version} runtime={runtime_version or 'unavailable'} "
        f"migration={migration} quick_check={','.join(quick[:3])}"
    )
    return CheckResult("condition_1_identity_db", Verdict.PASS if ok else Verdict.FAIL, detail)


def _graph_lineage_errors(connection: sqlite3.Connection) -> int:
    query = """
        SELECT COUNT(*) FROM graph_observation_supports gos
        LEFT JOIN graph_observations go ON go.observation_claim_id=gos.observation_claim_id
        LEFT JOIN claims sc ON sc.id=gos.supporting_claim_id
        LEFT JOIN evidence_items ei ON ei.id=gos.evidence_item_id
        LEFT JOIN source_items si ON si.id=gos.source_item_id
        LEFT JOIN entity_edges ee ON ee.source_id=gos.source_entity_id
            AND ee.target_id=gos.target_entity_id AND ee.relation=gos.relation
        WHERE go.observation_claim_id IS NULL OR sc.id IS NULL OR ei.id IS NULL
           OR si.id IS NULL OR ee.source_id IS NULL
    """
    return int(connection.execute(query).fetchone()[0])


def _current_graph_diagnostics(db: Path) -> tuple[int, int, int]:
    from collections import defaultdict

    from memorymaster.knowledge.graph_observation_repository import _observation_support
    from memorymaster.knowledge.graph_observations import discover_components
    from memorymaster.knowledge.ontology import load_ontology

    ontology = load_ontology()
    relations = tuple(sorted(ontology.relations))
    marks = ",".join("?" for _ in relations)
    with _connect_ro(db) as connection:
        scope_count = int(connection.execute(
            "SELECT COUNT(DISTINCT scope) FROM entity_edge_supports"
        ).fetchone()[0])
        rows = connection.execute(f"""
            SELECT ees.supporting_claim_id AS claim_id,
                   cel.evidence_item_id AS evidence_id,
                   e.source_item_id, ees.source_entity_id, ees.relation,
                   ees.target_entity_id, ees.ontology_version, c.scope,
                   c.tenant_id, c.confidence, s.occurred_at
            FROM entity_edge_supports ees
            JOIN claims c ON c.id=ees.supporting_claim_id
            JOIN claim_evidence_links cel ON cel.claim_id=c.id
            JOIN evidence_items e ON e.id=cel.evidence_item_id
            JOIN source_items s ON s.id=e.source_item_id
            WHERE ees.scope=c.scope AND c.status='confirmed'
              AND c.visibility<>'sensitive'
              AND COALESCE(c.claim_type, '') NOT IN ('observation','skill','summary')
              AND COALESCE(c.source_agent, '')<>'memorymaster-graph-observer'
              AND s.retired_at IS NULL AND s.sensitivity='none' AND e.sensitivity='none'
              AND ees.ontology_version=? AND ees.relation IN ({marks})
            ORDER BY c.scope, c.tenant_id, cel.evidence_item_id, c.id,
                     ees.source_entity_id, ees.relation, ees.target_entity_id
        """, (ontology.version, *relations)).fetchall()
    symmetric = frozenset(name for name, definition in ontology.relations.items() if definition.symmetric)
    grouped: dict[tuple[str, str | None], list[Any]] = defaultdict(list)
    for row in rows:
        support = _observation_support(row, symmetric)
        grouped[(support.scope, support.tenant_id)].append(support)
    components = diagnostics = 0
    for (scope, tenant_id), supports in grouped.items():
        result = discover_components(supports, scope=scope, tenant_id=tenant_id)
        components += len(result.components)
        diagnostics += len(result.diagnostics)
    return scope_count, components, diagnostics


def check_graph_observations(config: GateConfig) -> CheckResult:
    try:
        with _connect_ro(config.db) as connection:
            marks = ",".join("?" for _ in ACTIVE_OBSERVATION_STATES)
            backlog = int(connection.execute(
                f"SELECT COUNT(*) FROM graph_observation_jobs WHERE status IN ({marks})",
                ACTIVE_OBSERVATION_STATES,
            ).fetchone()[0])
            completed = int(connection.execute(
                "SELECT COUNT(*) FROM graph_observation_jobs WHERE stage='discover' AND status='completed'"
            ).fetchone()[0])
            observations = int(connection.execute("SELECT COUNT(*) FROM graph_observations").fetchone()[0])
            lineage_errors = _graph_lineage_errors(connection)
        scopes, eligible, diagnostics = _current_graph_diagnostics(config.db)
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        return CheckResult("condition_2_graph_observations", Verdict.FAIL, f"probe_error={type(exc).__name__}")
    empty_proven = observations > 0 or (completed > 0 and scopes > 0 and eligible == 0)
    ok = backlog == 0 and lineage_errors == 0 and empty_proven
    detail = (
        f"backlog={backlog} completed_discovery={completed} observations={observations} "
        f"current_scopes={scopes} eligible_components={eligible} diagnostics={diagnostics} "
        f"lineage_errors={lineage_errors} empty_proven={str(empty_proven).lower()}"
    )
    return CheckResult("condition_2_graph_observations", Verdict.PASS if ok else Verdict.FAIL, detail)


def _profile_marker_emitted(hook: Path, db: Path) -> bool:
    if not hook.is_file():
        return False
    env = dict(os.environ)
    env["MEMORYMASTER_DEFAULT_DB"] = str(db)
    try:
        run = subprocess.run(
            [sys.executable, str(hook)],
            input="{}",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=30,
            check=False,
        )
        payload = json.loads(run.stdout)
        context = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return False
    return run.returncode == 0 and "MemoryMaster compiled user profile" in str(context)


def check_compiled_profile(config: GateConfig) -> CheckResult:
    try:
        with _connect_ro(config.db) as connection:
            active_run = int(connection.execute(
                "SELECT COUNT(*) FROM compiled_profile_runs WHERE status='completed'"
            ).fetchone()[0])
            facts = connection.execute(
                "SELECT COUNT(*), MIN(independent_sessions), MIN(support_count) "
                "FROM compiled_profile_facts WHERE status='active'"
            ).fetchone()
            mismatch = int(connection.execute("""
                SELECT COUNT(*) FROM compiled_profile_facts f
                LEFT JOIN (
                    SELECT fact_id, COUNT(*) supports, COUNT(DISTINCT session_id) sessions
                    FROM compiled_profile_supports GROUP BY fact_id
                ) s ON s.fact_id=f.id
                WHERE f.status='active' AND (
                    f.support_count<>COALESCE(s.supports,0)
                    OR f.independent_sessions<>COALESCE(s.sessions,0)
                )
            """).fetchone()[0])
        marker = _profile_marker_emitted(config.session_hook, config.db)
    except (OSError, sqlite3.Error) as exc:
        return CheckResult("condition_3_compiled_profile", Verdict.FAIL, f"probe_error={type(exc).__name__}")
    count, min_sessions, min_supports = map(lambda value: int(value or 0), facts)
    ok = active_run == 1 and count > 0 and min_sessions >= 2 and min_supports >= 2 and mismatch == 0 and marker
    detail = (
        f"active_run={active_run} active_facts={count} min_sessions={min_sessions} "
        f"min_supports={min_supports} support_mismatches={mismatch} session_marker={str(marker).lower()}"
    )
    return CheckResult("condition_3_compiled_profile", Verdict.PASS if ok else Verdict.FAIL, detail)


def _http_status(url: str) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - fixed local operator URL by default
            return int(response.status)
    except (OSError, urllib.error.URLError):
        return None


def _task_state() -> dict[str, dict[str, Any]]:
    if os.name != "nt":
        return {}
    quoted = ",".join(f"'{name}'" for name in TASK_NAMES)
    command = (
        "function fmt($v){if($null -eq $v){return ''}; return $v.ToString('o')}; "
        f"$names=@({quoted}); $rows=@(foreach($n in $names){{"
        "$t=Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue; if($t){"
        "$i=Get-ScheduledTaskInfo -TaskName $n; [pscustomobject]@{name=$n;enabled=[bool]$t.Settings.Enabled;"
        "state=[string]$t.State;last_run=(fmt $i.LastRunTime);next_run=(fmt $i.NextRunTime);"
        "last_result=[int64]$i.LastTaskResult}}}); $rows|ConvertTo-Json -Compress"
    )
    run = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    if run.returncode != 0 or not run.stdout.strip():
        return {}
    payload = json.loads(run.stdout)
    rows = payload if isinstance(payload, list) else [payload]
    return {str(row["name"]): row for row in rows}


def _read_receipts(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def checkpoint_result(
    task_name: str,
    state: dict[str, Any] | None,
    receipts: Iterable[dict[str, Any]],
    now: datetime,
) -> CheckResult:
    label = "condition_4_checkpoint_" + ("daily" if task_name.endswith("Daily") else "weekly")
    if not state or not state.get("enabled"):
        return CheckResult(label, Verdict.FAIL, "scheduled task missing or disabled")
    last_run = _parse_time(str(state.get("last_run") or ""))
    never_ran = last_run is None or last_run.year < 2001
    due = _parse_time(str(state.get("next_run") or "")) if never_ran else last_run
    valid = [
        row for row in receipts
        if row.get("task") == task_name
        and row.get("work_performed") is True
        and str(row.get("result", "")).lower() == "pass"
        and _parse_time(str(row.get("completed_at") or "")) is not None
    ]
    if valid:
        return CheckResult(label, Verdict.PASS, f"real_work_receipts={len(valid)}")
    if never_ran and due is not None and now < due:
        due_text = due.isoformat()
        return CheckResult(label, Verdict.NOT_YET_DUE, "first natural fire has not occurred", due_text)
    due_text = due.isoformat() if due is not None else "unknown"
    return CheckResult(label, Verdict.FAIL, f"no real-work receipt after due={due_text}", due_text)


def check_runtime(config: GateConfig, *, now: datetime | None = None) -> list[CheckResult]:
    health = _http_status(config.base_url.rstrip("/") + "/healthz")
    ready = _http_status(config.base_url.rstrip("/") + "/readyz")
    try:
        tasks = _task_state()
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        tasks = {}
    enabled = all(name in tasks and bool(tasks[name].get("enabled")) for name in TASK_NAMES)
    base_ok = health == 200 and ready == 200 and enabled
    base = CheckResult(
        "condition_4_runtime",
        Verdict.PASS if base_ok else Verdict.FAIL,
        f"healthz={health} readyz={ready} enabled_tasks={sum(name in tasks and bool(tasks[name].get('enabled')) for name in TASK_NAMES)}/{len(TASK_NAMES)}",
    )
    receipts = _read_receipts(config.receipt_file)
    stamp = now or _utc_now()
    checkpoints = [checkpoint_result(name, tasks.get(name), receipts, stamp) for name in CHECKPOINT_TASKS]
    return [base, *checkpoints]


def _percentile_95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _retrieval_once(db: Path, query: str, limit: int) -> list[str]:
    from memorymaster.core.service import MemoryService
    from memorymaster.recall.planner import RetrievalRequest, build_retrieval_plan

    service = MemoryService(str(db), workspace_root=db.parent, read_only=True)
    plan = build_retrieval_plan(RetrievalRequest(query_text=query, limit=limit, trust_mode="trusted"))
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
    config: GateConfig,
    *,
    retrieve: Callable[[Path, str, int], list[str]] = _retrieval_once,
) -> CheckResult:
    timings: list[float] = []
    rankings: list[list[str]] = []
    try:
        for _ in range(config.retrieval_samples):
            started = time.perf_counter()
            rankings.append(retrieve(config.db, TARGET_QUERY, 5))
            timings.append(time.perf_counter() - started)
    except Exception as exc:  # noqa: BLE001 - gate converts probe failures into a typed verdict
        return CheckResult("condition_5_natural_language_retrieval", Verdict.FAIL, f"probe_error={type(exc).__name__}")
    p95 = _percentile_95(timings)
    target_hits = sum(TARGET_HUMAN_ID in ranking for ranking in rankings)
    ok = target_hits == len(rankings) and p95 <= config.retrieval_p95_seconds
    detail = (
        f"target={TARGET_HUMAN_ID} hits={target_hits}/{len(rankings)} "
        f"p95_seconds={p95:.3f} budget_seconds={config.retrieval_p95_seconds:.3f} "
        f"last_top5={','.join(rankings[-1])}"
    )
    return CheckResult("condition_5_natural_language_retrieval", Verdict.PASS if ok else Verdict.FAIL, detail)


def exit_code(results: Iterable[CheckResult]) -> int:
    verdicts = {result.verdict for result in results}
    if Verdict.FAIL in verdicts:
        return 1
    if Verdict.NOT_YET_DUE in verdicts:
        return 3
    return 0


def run_gate(config: GateConfig, *, now: datetime | None = None) -> list[CheckResult]:
    results = [
        check_identity_and_db(config),
        check_graph_observations(config),
        check_compiled_profile(config),
    ]
    results.extend(check_runtime(config, now=now))
    results.append(check_retrieval(config))
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db")
    parser.add_argument("--runtime-python")
    parser.add_argument("--base-url", default=os.environ.get("MEMORYMASTER_HTTP_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--receipt-file", default=str(Path.home() / ".memorymaster" / "checkpoints" / "work-receipts.jsonl"))
    parser.add_argument("--session-hook", default=str(Path.home() / ".claude" / "hooks" / "memorymaster-session-start.py"))
    parser.add_argument("--retrieval-samples", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    db = _discover_db(args.db)
    config = GateConfig(
        db=db,
        runtime_python=_discover_runtime_python(args.runtime_python, EXPECTED_VERSION),
        base_url=args.base_url,
        receipt_file=Path(args.receipt_file).expanduser(),
        session_hook=Path(args.session_hook).expanduser(),
        retrieval_samples=max(1, min(20, args.retrieval_samples)),
    )
    results = run_gate(config)
    code = exit_code(results)
    if args.json:
        print(json.dumps({"exit_code": code, "checks": [{**asdict(item), "verdict": item.verdict.value} for item in results]}, indent=2))
    else:
        for item in results:
            due = f" due={item.due_at}" if item.due_at else ""
            print(f"{item.name}: {item.verdict.value}{due} - {item.detail}")
        label = {0: "ACCEPTED", 1: "FAILED", 3: "INCOMPLETE"}[code]
        print(f"overall: {label} (exit {code})")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
