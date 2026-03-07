from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memorymaster.service import MemoryService

DEFAULT_PERF_SLO_CONFIG = "benchmarks/slo_targets.json"
DEFAULT_ARTIFACTS_ROOT = "artifacts/incident_drill"
DEFAULT_E2E_REPORT = "artifacts/e2e/operator_e2e_report.json"
DEFAULT_COMPACTION_ARTIFACTS_DIR = "artifacts/compaction"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_drill_id() -> str:
    return datetime.now(timezone.utc).strftime("drill-%Y%m%dT%H%M%SZ")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        return raw
    return {}


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _resolve_path(path_like: str, *, workspace_root: Path) -> Path:
    path = Path(path_like)
    if path.is_absolute():
        return path
    return workspace_root / path


def _append_if_value(argv: list[str], flag: str, value: str) -> None:
    if _to_str(value).strip():
        argv.extend([flag, str(value)])


@dataclass(slots=True)
class CommandSpec:
    name: str
    argv: list[str]
    expected_artifacts: list[str]


@dataclass(slots=True)
class CommandExecution:
    name: str
    argv: list[str]
    returncode: int
    duration_seconds: float
    stdout_path: str
    stderr_path: str
    expected_artifacts: list[str]


def build_command_plan(
    *,
    python_executable: str,
    workspace_root: Path,
    drill_dir: Path,
    perf_slo_config: str,
    perf_claims: int,
    perf_queries: int,
    perf_cycles: int,
    include_operator_e2e: bool,
    eval_strict: bool,
) -> list[CommandSpec]:
    perf_json = drill_dir / "perf" / "perf_smoke.json"
    eval_json = drill_dir / "eval" / "eval_results.json"
    eval_md = drill_dir / "eval" / "eval_report.md"
    eval_csv = drill_dir / "eval" / "eval_checks.csv"
    eval_db_dir = drill_dir / "eval" / "db"

    plan = [
        CommandSpec(
            name="perf_smoke",
            argv=[
                python_executable,
                "benchmarks/perf_smoke.py",
                "--workspace",
                str(workspace_root),
                "--out-json",
                str(perf_json),
                "--slo-config",
                perf_slo_config,
                "--claims",
                str(perf_claims),
                "--queries",
                str(perf_queries),
                "--cycles",
                str(perf_cycles),
            ],
            expected_artifacts=[str(perf_json)],
        ),
        CommandSpec(
            name="eval_memorymaster",
            argv=[
                python_executable,
                "scripts/eval_memorymaster.py",
                "--benchmarks",
                "benchmarks/cases.jsonl,benchmarks/cases_general.jsonl,benchmarks/cases_adversarial.jsonl",
                "--db-dir",
                str(eval_db_dir),
                "--out-json",
                str(eval_json),
                "--out-md",
                str(eval_md),
                "--out-csv",
                str(eval_csv),
                *(["--strict"] if eval_strict else []),
            ],
            expected_artifacts=[str(eval_json), str(eval_md), str(eval_csv)],
        ),
    ]
    if include_operator_e2e:
        plan.append(
            CommandSpec(
                name="operator_e2e",
                argv=[python_executable, "scripts/e2e_operator.py"],
                expected_artifacts=[DEFAULT_E2E_REPORT],
            )
        )
    return plan


def run_command(spec: CommandSpec, *, cwd: Path, output_dir: Path, index: int) -> CommandExecution:
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{index:02d}_{spec.name}"
    stdout_path = output_dir / f"{safe_name}.stdout.txt"
    stderr_path = output_dir / f"{safe_name}.stderr.txt"

    started = time.monotonic()
    proc = subprocess.run(
        spec.argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    duration = round(time.monotonic() - started, 4)
    stdout_path.write_text(proc.stdout or "", encoding="utf-8")
    stderr_path.write_text(proc.stderr or "", encoding="utf-8")
    return CommandExecution(
        name=spec.name,
        argv=spec.argv,
        returncode=int(proc.returncode),
        duration_seconds=duration,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        expected_artifacts=spec.expected_artifacts,
    )


def summarize_reconcile(report: dict[str, Any]) -> dict[str, int]:
    summary = report.get("summary")
    if not isinstance(summary, dict):
        return {}
    result: dict[str, int] = {}
    keys = (
        "orphan_events",
        "orphan_citations",
        "superseded_without_replacement",
        "hash_chain_issues",
        "invalid_transitions",
    )
    for key in keys:
        value = summary.get(key, 0)
        try:
            result[key] = int(value)
        except (TypeError, ValueError):
            result[key] = 0
    return result


def should_apply_fix(before: dict[str, Any], skip_fix: bool) -> bool:
    if skip_fix:
        return False
    summary = summarize_reconcile(before)
    if not summary:
        return False
    return summary.get("orphan_events", 0) > 0 or summary.get("orphan_citations", 0) > 0 or summary.get("hash_chain_issues", 0) > 0


def build_evidence_markdown(
    *,
    drill_id: str,
    db_path: str,
    workspace_root: str,
    command_rows: list[CommandExecution],
    artifacts: dict[str, str],
    before_summary: dict[str, int],
    after_summary: dict[str, int],
    perf_report: dict[str, Any],
) -> str:
    ingest_threshold = perf_report.get("thresholds", {}).get("ingest_p95_seconds_max")
    query_threshold = perf_report.get("thresholds", {}).get("query_p95_seconds_max")
    cycle_threshold = perf_report.get("thresholds", {}).get("cycle_p95_seconds_max")
    runtime_threshold = perf_report.get("thresholds", {}).get("total_runtime_seconds_max")
    ingest_observed = perf_report.get("metrics", {}).get("timing", {}).get("ingest", {}).get("p95_seconds")
    query_observed = perf_report.get("metrics", {}).get("timing", {}).get("query", {}).get("p95_seconds")
    cycle_observed = perf_report.get("metrics", {}).get("timing", {}).get("cycle", {}).get("p95_seconds")
    runtime_observed = perf_report.get("metrics", {}).get("timing", {}).get("total_runtime_seconds")

    lines: list[str] = []
    lines.append(f"# Incident Drill Evidence ({drill_id})")
    lines.append("")
    lines.append("## Drill Metadata")
    lines.append(f"- drill_id: {drill_id}")
    lines.append(f"- date_utc: {utc_now()}")
    lines.append("- environment: local")
    lines.append("- operator: memorymaster")
    lines.append(f"- db_path: {db_path}")
    lines.append(f"- workspace_root: {workspace_root}")
    lines.append("")
    lines.append("## Commands Executed")
    lines.append("```text")
    for row in command_rows:
        lines.append(" ".join(row.argv))
    lines.append("```")
    lines.append("")
    lines.append("## Artifact Evidence")
    lines.append(f"- perf_smoke_json: {artifacts.get('perf_smoke_json', '')}")
    lines.append(f"- eval_results_json: {artifacts.get('eval_results_json', '')}")
    lines.append(f"- eval_report_md: {artifacts.get('eval_report_md', '')}")
    lines.append(f"- reconciliation_report_before: {artifacts.get('reconcile_before_json', '')}")
    lines.append(f"- reconciliation_report_after: {artifacts.get('reconcile_after_json', '')}")
    lines.append(f"- operator_e2e_report_json: {artifacts.get('operator_e2e_report_json', '')}")
    lines.append(f"- compaction_trace_validation_json: {artifacts.get('compaction_trace_validation_json', '')}")
    lines.append(f"- drill_signoff_json: {artifacts.get('drill_signoff_json', '')}")
    lines.append("")
    lines.append("## Reconciliation Before/After")
    lines.append("")
    lines.append("| Metric | Before | After |")
    lines.append("| --- | --- | --- |")
    lines.append(f"| orphan_events | {before_summary.get('orphan_events', 0)} | {after_summary.get('orphan_events', 0)} |")
    lines.append(f"| orphan_citations | {before_summary.get('orphan_citations', 0)} | {after_summary.get('orphan_citations', 0)} |")
    lines.append(
        f"| superseded_without_replacement | {before_summary.get('superseded_without_replacement', 0)} | {after_summary.get('superseded_without_replacement', 0)} |"
    )
    lines.append(f"| hash_chain_issues | {before_summary.get('hash_chain_issues', 0)} | {after_summary.get('hash_chain_issues', 0)} |")
    lines.append(f"| invalid_transitions | {before_summary.get('invalid_transitions', 0)} | {after_summary.get('invalid_transitions', 0)} |")
    lines.append("")
    lines.append("## Performance Summary")
    lines.append("")
    lines.append("| Metric | Threshold | Observed |")
    lines.append("| --- | --- | --- |")
    lines.append(f"| ingest_p95_seconds | <= {ingest_threshold} | {ingest_observed} |")
    lines.append(f"| query_p95_seconds | <= {query_threshold} | {query_observed} |")
    lines.append(f"| cycle_p95_seconds | <= {cycle_threshold} | {cycle_observed} |")
    lines.append(f"| wall_clock_seconds | <= {runtime_threshold} | {runtime_observed} |")
    lines.append("")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an end-to-end incident drill and collect evidence artifacts.")
    parser.add_argument("--db", default="memorymaster.db", help="SQLite DB path or Postgres DSN.")
    parser.add_argument("--workspace", default=".", help="Workspace root for deterministic checks.")
    parser.add_argument("--artifacts-root", default=DEFAULT_ARTIFACTS_ROOT, help="Root directory for drill artifacts.")
    parser.add_argument("--drill-id", default="", help="Override drill id (default uses UTC timestamp).")
    parser.add_argument("--python-executable", default=sys.executable, help="Python executable used for child commands.")
    parser.add_argument("--perf-slo-config", default=DEFAULT_PERF_SLO_CONFIG, help="Path to perf smoke SLO config JSON.")
    parser.add_argument("--perf-claims", type=int, default=80, help="Perf smoke ingest count.")
    parser.add_argument("--perf-queries", type=int, default=30, help="Perf smoke query count.")
    parser.add_argument("--perf-cycles", type=int, default=2, help="Perf smoke cycle count.")
    parser.add_argument("--reconcile-limit", type=int, default=500, help="Integrity reconciliation scan limit.")
    parser.add_argument("--skip-integrity-fix", action="store_true", help="Skip reconcile_integrity(fix=True).")
    parser.add_argument("--skip-operator-e2e", action="store_true", help="Skip operator e2e harness command.")
    parser.add_argument(
        "--compaction-artifacts-dir",
        default=DEFAULT_COMPACTION_ARTIFACTS_DIR,
        help="Compaction artifacts directory (workspace-relative unless absolute).",
    )
    parser.add_argument(
        "--skip-compaction-trace-validation",
        action="store_true",
        help="Skip compaction traceability artifact validation command.",
    )
    parser.add_argument(
        "--strict-compaction-trace-validation",
        action="store_true",
        help="Treat missing compaction artifacts as drill failures.",
    )
    parser.add_argument(
        "--skip-drill-signoff",
        action="store_true",
        help="Skip generation of drill signoff artifact.",
    )
    parser.add_argument(
        "--strict-drill-signoff-missing",
        action="store_true",
        help="Fail signoff generation when referenced artifacts are missing.",
    )
    parser.add_argument(
        "--strict-drill-signoff-complete",
        action="store_true",
        help="Fail the drill unless drill signoff approval fields are complete.",
    )
    parser.add_argument(
        "--strict-drill-signoff-signed",
        action="store_true",
        help="Fail the drill unless drill signoff is signed in HMAC mode.",
    )
    parser.add_argument(
        "--signoff-json",
        default="",
        help="Optional explicit drill signoff output path (defaults under drill folder).",
    )
    parser.add_argument("--approver-name", default="", help="Drill signoff approver name.")
    parser.add_argument("--approver-email", default="", help="Drill signoff approver email.")
    parser.add_argument("--approver-role", default="", help="Drill signoff approver role.")
    parser.add_argument("--approver-decision", default="", help="Drill signoff decision.")
    parser.add_argument("--approval-ticket", default="", help="Drill signoff ticket/change request id.")
    parser.add_argument("--approval-notes", default="", help="Drill signoff notes.")
    parser.add_argument(
        "--signing-key",
        default="",
        help="Optional explicit signing key for signoff artifact generation.",
    )
    parser.add_argument(
        "--signing-key-env",
        default="MEMORYMASTER_DRILL_SIGNING_KEY",
        help="Environment variable containing signoff signing key.",
    )
    parser.add_argument("--no-eval-strict", action="store_true", help="Run eval without strict exit gating.")
    parser.add_argument("--dry-run", action="store_true", help="Write command plan only; do not execute commands.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path.cwd()
    workspace_root = Path(args.workspace)
    drill_id = args.drill_id.strip() or default_drill_id()
    drill_dir = Path(args.artifacts_root) / drill_id
    command_output_dir = drill_dir / "commands"
    reconcile_dir = drill_dir / "reconcile"
    perf_json = drill_dir / "perf" / "perf_smoke.json"
    eval_json = drill_dir / "eval" / "eval_results.json"
    eval_md = drill_dir / "eval" / "eval_report.md"
    eval_csv = drill_dir / "eval" / "eval_checks.csv"
    operator_report_in_drill = drill_dir / "operator" / "operator_e2e_report.json"
    compaction_validation_json = drill_dir / "compaction" / "compaction_trace_validation.json"
    if _to_str(args.signoff_json).strip():
        drill_signoff_json = Path(args.signoff_json)
        if not drill_signoff_json.is_absolute():
            drill_signoff_json = drill_dir / drill_signoff_json
    else:
        drill_signoff_json = drill_dir / "signoff" / "drill_signoff.json"
    evidence_md = drill_dir / "incident_drill_evidence.md"
    run_summary_json = drill_dir / "incident_drill_run.json"
    reconcile_before_json = reconcile_dir / "reconcile_before.json"
    reconcile_fix_json = reconcile_dir / "reconcile_fix.json"
    reconcile_after_json = reconcile_dir / "reconcile_after.json"
    compaction_artifacts_dir = _resolve_path(str(args.compaction_artifacts_dir), workspace_root=workspace_root)

    drill_dir.mkdir(parents=True, exist_ok=True)
    core_command_plan = build_command_plan(
        python_executable=str(args.python_executable),
        workspace_root=workspace_root,
        drill_dir=drill_dir,
        perf_slo_config=str(args.perf_slo_config),
        perf_claims=int(args.perf_claims),
        perf_queries=int(args.perf_queries),
        perf_cycles=int(args.perf_cycles),
        include_operator_e2e=not args.skip_operator_e2e,
        eval_strict=not args.no_eval_strict,
    )
    post_command_plan: list[CommandSpec] = []
    if not args.skip_compaction_trace_validation:
        validate_argv = [
            str(args.python_executable),
            "scripts/compaction_trace_validate.py",
            "--artifacts-dir",
            str(compaction_artifacts_dir),
            "--out-json",
            str(compaction_validation_json),
        ]
        if not args.strict_compaction_trace_validation:
            validate_argv.append("--allow-missing")
        post_command_plan.append(
            CommandSpec(
                name="compaction_trace_validate",
                argv=validate_argv,
                expected_artifacts=[str(compaction_validation_json)],
            )
        )
    if not args.skip_drill_signoff:
        signoff_argv = [
            str(args.python_executable),
            "scripts/generate_drill_signoff.py",
            "--run-summary",
            str(run_summary_json),
            "--workspace-root",
            str(workspace_root),
            "--evidence-md",
            str(evidence_md),
            "--out-json",
            str(drill_signoff_json),
            "--signing-key-env",
            str(args.signing_key_env),
        ]
        _append_if_value(signoff_argv, "--approver-name", str(args.approver_name))
        _append_if_value(signoff_argv, "--approver-email", str(args.approver_email))
        _append_if_value(signoff_argv, "--approver-role", str(args.approver_role))
        _append_if_value(signoff_argv, "--decision", str(args.approver_decision))
        _append_if_value(signoff_argv, "--approval-ticket", str(args.approval_ticket))
        _append_if_value(signoff_argv, "--approval-notes", str(args.approval_notes))
        _append_if_value(signoff_argv, "--signing-key", str(args.signing_key))
        if args.strict_drill_signoff_missing:
            signoff_argv.append("--strict-missing")
        post_command_plan.append(
            CommandSpec(
                name="drill_signoff",
                argv=signoff_argv,
                expected_artifacts=[str(drill_signoff_json)],
            )
        )

    artifacts = {
        "perf_smoke_json": str(perf_json),
        "eval_results_json": str(eval_json),
        "eval_report_md": str(eval_md),
        "eval_checks_csv": str(eval_csv),
        "reconcile_before_json": str(reconcile_before_json),
        "reconcile_fix_json": str(reconcile_fix_json),
        "reconcile_after_json": str(reconcile_after_json),
        "operator_e2e_report_json": str(operator_report_in_drill),
        "compaction_trace_validation_json": str(compaction_validation_json),
        "drill_signoff_json": str(drill_signoff_json),
        "incident_drill_evidence_md": str(evidence_md),
        "incident_drill_run_json": str(run_summary_json),
    }

    if args.dry_run:
        dry_payload = {
            "timestamp": utc_now(),
            "status": "dry_run",
            "drill_id": drill_id,
            "drill_dir": str(drill_dir),
            "command_plan": [asdict(item) for item in [*core_command_plan, *post_command_plan]],
            "artifacts": artifacts,
        }
        write_json(run_summary_json, dry_payload)
        print(json.dumps(dry_payload, indent=2))
        return 0

    service = MemoryService(args.db, workspace_root=workspace_root)
    service.init_db()

    reconcile_before = service.store.reconcile_integrity(fix=False, limit=int(args.reconcile_limit))
    write_json(reconcile_before_json, reconcile_before)

    command_results: list[CommandExecution] = []
    failed_command: str | None = None
    command_index = 0
    for spec in core_command_plan:
        command_index += 1
        result = run_command(spec, cwd=repo_root, output_dir=command_output_dir, index=command_index)
        command_results.append(result)
        if result.returncode != 0:
            failed_command = spec.name
            break

    reconcile_fix: dict[str, Any] = {"skipped": True}
    if should_apply_fix(reconcile_before, bool(args.skip_integrity_fix)):
        reconcile_fix = service.store.reconcile_integrity(fix=True, limit=int(args.reconcile_limit))
        write_json(reconcile_fix_json, reconcile_fix)
    else:
        write_json(
            reconcile_fix_json,
            {
                "skipped": True,
                "reason": "skip_integrity_fix flag set or no orphan/hash-chain issues detected",
            },
        )

    reconcile_after = service.store.reconcile_integrity(fix=False, limit=int(args.reconcile_limit))
    write_json(reconcile_after_json, reconcile_after)

    signoff_spec: CommandSpec | None = None
    for spec in post_command_plan:
        if spec.name == "drill_signoff":
            signoff_spec = spec
            continue
        command_index += 1
        result = run_command(spec, cwd=repo_root, output_dir=command_output_dir, index=command_index)
        command_results.append(result)
        if result.returncode != 0 and failed_command is None:
            failed_command = spec.name

    source_e2e_report = repo_root / DEFAULT_E2E_REPORT
    if source_e2e_report.exists():
        operator_report_in_drill.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_e2e_report, operator_report_in_drill)

    perf_report = read_json_if_exists(perf_json)
    eval_report = read_json_if_exists(eval_json)
    operator_report = read_json_if_exists(operator_report_in_drill)
    compaction_validation_report = read_json_if_exists(compaction_validation_json)

    compaction_validation_status = _to_str(compaction_validation_report.get("status")).strip().lower()
    if args.skip_compaction_trace_validation:
        compaction_validation_status = "skipped"
    compaction_validation_ok = bool(args.skip_compaction_trace_validation) or (
        compaction_validation_status in {"pass", "skipped"}
    )

    evidence = build_evidence_markdown(
        drill_id=drill_id,
        db_path=str(args.db),
        workspace_root=str(workspace_root),
        command_rows=command_results,
        artifacts=artifacts,
        before_summary=summarize_reconcile(reconcile_before),
        after_summary=summarize_reconcile(reconcile_after),
        perf_report=perf_report,
    )
    evidence_md.parent.mkdir(parents=True, exist_ok=True)
    evidence_md.write_text(evidence, encoding="utf-8")

    pre_signoff_payload = {
        "timestamp": utc_now(),
        "status": "in_progress",
        "drill_id": drill_id,
        "drill_dir": str(drill_dir),
        "db_path": str(args.db),
        "workspace_root": str(workspace_root),
        "commands": [asdict(item) for item in command_results],
        "failed_command": failed_command,
        "gates": {
            "commands_ok": all(item.returncode == 0 for item in command_results) and failed_command is None,
            "compaction_validation_ok": compaction_validation_ok,
        },
        "reconciliation": {
            "before": summarize_reconcile(reconcile_before),
            "fix": reconcile_fix if isinstance(reconcile_fix, dict) else {},
            "after": summarize_reconcile(reconcile_after),
        },
        "artifacts": artifacts,
    }
    write_json(run_summary_json, pre_signoff_payload)

    if signoff_spec is not None:
        command_index += 1
        signoff_result = run_command(signoff_spec, cwd=repo_root, output_dir=command_output_dir, index=command_index)
        command_results.append(signoff_result)
        if signoff_result.returncode != 0 and failed_command is None:
            failed_command = signoff_spec.name

    signoff_report = read_json_if_exists(drill_signoff_json)
    signoff_status = _to_str(signoff_report.get("status")).strip().lower()
    if args.skip_drill_signoff:
        signoff_status = "skipped"
    signoff_complete = bool(signoff_report.get("approval", {}).get("complete")) if signoff_report else False
    signoff_signature_algorithm = _to_str(signoff_report.get("signature", {}).get("algorithm")).strip().lower()
    signoff_signed = bool(signoff_report.get("signature", {}).get("signed")) if signoff_report else False
    require_signoff_complete = bool(args.strict_drill_signoff_complete)
    require_signoff_signed = bool(args.strict_drill_signoff_signed)
    signoff_complete_ok = (not require_signoff_complete) or signoff_complete
    signoff_signed_ok = (not require_signoff_signed) or (signoff_signed and signoff_signature_algorithm == "hmac-sha256")

    commands_ok = all(item.returncode == 0 for item in command_results) and failed_command is None
    perf_ok = bool(perf_report.get("passed", False)) if perf_report else False
    eval_ok = bool(eval_report.get("passed", False)) if eval_report else False
    operator_ok = True
    if not args.skip_operator_e2e:
        operator_ok = operator_report.get("summary", {}).get("status") == "pass"

    status = (
        "pass"
        if (commands_ok and perf_ok and eval_ok and operator_ok and compaction_validation_ok and signoff_complete_ok and signoff_signed_ok)
        else "fail"
    )
    run_payload = {
        "timestamp": utc_now(),
        "status": status,
        "drill_id": drill_id,
        "drill_dir": str(drill_dir),
        "db_path": str(args.db),
        "workspace_root": str(workspace_root),
        "commands": [asdict(item) for item in command_results],
        "failed_command": failed_command,
        "gates": {
            "commands_ok": commands_ok,
            "perf_ok": perf_ok,
            "eval_ok": eval_ok,
            "operator_ok": operator_ok,
            "compaction_validation_ok": compaction_validation_ok,
            "signoff_generated": bool(args.skip_drill_signoff or signoff_report),
            "signoff_complete": signoff_complete,
            "signoff_signed": signoff_signed,
            "signoff_signature_hmac": signoff_signature_algorithm == "hmac-sha256",
            "require_signoff_complete": require_signoff_complete,
            "require_signoff_signed": require_signoff_signed,
            "signoff_complete_ok": signoff_complete_ok,
            "signoff_signed_ok": signoff_signed_ok,
        },
        "reconciliation": {
            "before": summarize_reconcile(reconcile_before),
            "fix": reconcile_fix if isinstance(reconcile_fix, dict) else {},
            "after": summarize_reconcile(reconcile_after),
        },
        "automation": {
            "compaction_trace_validation": {
                "status": compaction_validation_status or ("skipped" if args.skip_compaction_trace_validation else "unknown"),
                "passed": bool(compaction_validation_report.get("passed", False))
                if compaction_validation_report
                else bool(args.skip_compaction_trace_validation),
                "report_json": str(compaction_validation_json),
            },
            "drill_signoff": {
                "status": signoff_status or ("skipped" if args.skip_drill_signoff else "unknown"),
                "approval_complete": signoff_complete,
                "signed": signoff_signed,
                "signature_algorithm": signoff_signature_algorithm,
                "required_complete": require_signoff_complete,
                "required_signed": require_signoff_signed,
                "complete_ok": signoff_complete_ok,
                "signed_ok": signoff_signed_ok,
                "report_json": str(drill_signoff_json),
            },
        },
        "artifacts": artifacts,
    }
    write_json(run_summary_json, run_payload)
    print(json.dumps(run_payload, indent=2))
    return 0 if status == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
