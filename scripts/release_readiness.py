from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


@dataclass
class CheckResult:
    name: str
    critical: bool
    status: str
    duration_ms: int
    exit_code: int | None = None
    summary: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _tail(text: str, limit: int = 2000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def _run_cmd(
    *,
    name: str,
    cmd: list[str],
    critical: bool,
    timeout_seconds: int,
    must_contain: list[str] | None = None,
) -> CheckResult:
    start = time.monotonic()
    try:
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        return CheckResult(
            name=name,
            critical=critical,
            status="timeout",
            duration_ms=duration_ms,
            exit_code=None,
            summary=f"timed out after {timeout_seconds}s",
            stdout_tail=_tail((exc.stdout or "") if isinstance(exc.stdout, str) else ""),
            stderr_tail=_tail((exc.stderr or "") if isinstance(exc.stderr, str) else ""),
        )

    duration_ms = int((time.monotonic() - start) * 1000)
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    ok = completed.returncode == 0
    missing_tokens: list[str] = []
    for token in must_contain or []:
        if token not in stdout and token not in stderr:
            missing_tokens.append(token)
    if missing_tokens:
        ok = False
    summary = f"exit={completed.returncode}"
    if missing_tokens:
        summary += f"; missing tokens={missing_tokens}"
    return CheckResult(
        name=name,
        critical=critical,
        status="pass" if ok else "fail",
        duration_ms=duration_ms,
        exit_code=completed.returncode,
        summary=summary,
        stdout_tail=_tail(stdout),
        stderr_tail=_tail(stderr),
    )


def _run_py_check(
    *,
    name: str,
    critical: bool,
    fn: Callable[[], tuple[bool, str]],
) -> CheckResult:
    start = time.monotonic()
    try:
        ok, summary = fn()
    except Exception as exc:  # pragma: no cover - defensive wrapper
        ok = False
        summary = f"exception: {exc}"
    duration_ms = int((time.monotonic() - start) * 1000)
    return CheckResult(
        name=name,
        critical=critical,
        status="pass" if ok else "fail",
        duration_ms=duration_ms,
        summary=summary,
    )


def _check_roadmap() -> tuple[bool, str]:
    path = Path("ROADMAP.md")
    if not path.exists():
        return False, "ROADMAP.md missing"
    text = path.read_text(encoding="utf-8", errors="replace")
    pending = sum(1 for line in text.splitlines() if line.strip().startswith("- [ ]"))
    partial = sum(1 for line in text.splitlines() if line.strip().startswith("- [~]"))
    checked = sum(1 for line in text.splitlines() if line.strip().startswith("- [x]"))
    ok = pending == 0 and partial == 0 and checked > 0
    return ok, f"checked={checked} pending={pending} partial={partial}"


def _check_required_files() -> tuple[bool, str]:
    required = [
        Path("README.md"),
        Path("USER_GUIDE.md"),
        Path("ROADMAP.md"),
        Path("memorymaster/mcp_server.py"),
        Path(".github/workflows/ci.yml"),
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        return False, f"missing={missing}"
    return True, f"present={len(required)}"


def _check_root_db_clean() -> tuple[bool, str]:
    db_files = [p.name for p in Path(".").glob("*.db")]
    if db_files:
        return False, f"root_db_count={len(db_files)} sample={db_files[:5]}"
    return True, "root_db_count=0"


def _compileall_cmd() -> list[str]:
    code = (
        "import compileall,sys;"
        "paths=['memorymaster','scripts','tests'];"
        "ok=all(compileall.compile_dir(p,quiet=1,maxlevels=10) for p in paths);"
        "sys.exit(0 if ok else 1)"
    )
    return [sys.executable, "-c", code]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(r, ensure_ascii=True) for r in rows) + "\n"
    path.write_text(payload, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run bounded production-readiness checks.")
    parser.add_argument(
        "--out-json",
        default="artifacts/release_readiness.json",
        help="Output report path.",
    )
    parser.add_argument(
        "--tmp-root",
        default=".tmp_cases/release_readiness",
        help="Temporary directory root for smoke checks.",
    )
    args = parser.parse_args()

    out_path = Path(args.out_json)
    tmp_root = Path(args.tmp_root)
    tmp_root.mkdir(parents=True, exist_ok=True)
    db_path = tmp_root / "release_smoke.db"
    inbox_path = tmp_root / "operator_inbox.jsonl"
    operator_log_path = tmp_root / "operator_events.jsonl"

    _write_jsonl(
        inbox_path,
        [
            {
                "session_id": "release",
                "thread_id": "release",
                "turn_id": "release-1",
                "user_text": "Support email is release@example.com",
                "assistant_text": "ack",
                "observations": [],
            }
        ],
    )

    checks: list[CheckResult] = []
    checks.append(_run_py_check(name="roadmap_status", critical=False, fn=_check_roadmap))
    checks.append(_run_py_check(name="required_files", critical=True, fn=_check_required_files))
    checks.append(_run_py_check(name="root_db_clean", critical=False, fn=_check_root_db_clean))

    checks.append(
        _run_cmd(
            name="compileall",
            cmd=_compileall_cmd(),
            critical=True,
            timeout_seconds=180,
        )
    )
    checks.append(
        _run_cmd(
            name="cli_init_db",
            cmd=[sys.executable, "-m", "memorymaster", "--db", str(db_path), "init-db"],
            critical=True,
            timeout_seconds=60,
        )
    )
    checks.append(
        _run_cmd(
            name="cli_ingest",
            cmd=[
                sys.executable,
                "-m",
                "memorymaster",
                "--db",
                str(db_path),
                "ingest",
                "--text",
                "Support email is release@example.com",
                "--source",
                "session://release|turn-1|seed",
            ],
            critical=True,
            timeout_seconds=60,
        )
    )
    checks.append(
        _run_cmd(
            name="cli_run_cycle",
            cmd=[
                sys.executable,
                "-m",
                "memorymaster",
                "--db",
                str(db_path),
                "run-cycle",
                "--policy-mode",
                "cadence",
            ],
            critical=True,
            timeout_seconds=60,
        )
    )
    checks.append(
        _run_cmd(
            name="cli_query",
            cmd=[
                sys.executable,
                "-m",
                "memorymaster",
                "--db",
                str(db_path),
                "query",
                "support email",
                "--retrieval-mode",
                "hybrid",
            ],
            critical=True,
            timeout_seconds=60,
            must_contain=["rows=1", "release@example.com"],
        )
    )
    checks.append(
        _run_cmd(
            name="operator_bounded_smoke",
            cmd=[
                sys.executable,
                "-m",
                "memorymaster",
                "--db",
                str(db_path),
                "--workspace",
                ".",
                "run-operator",
                "--inbox-jsonl",
                str(inbox_path),
                "--max-events",
                "1",
                "--max-idle-seconds",
                "5",
                "--poll-seconds",
                "0.1",
                "--retrieval-mode",
                "hybrid",
                "--policy-mode",
                "cadence",
                "--log-jsonl",
                str(operator_log_path),
            ],
            critical=True,
            timeout_seconds=90,
        )
    )
    checks.append(
        _run_cmd(
            name="mcp_server_help",
            cmd=[sys.executable, "-m", "memorymaster.mcp_server", "--help"],
            critical=True,
            timeout_seconds=60,
        )
    )
    checks.append(
        _run_cmd(
            name="incident_drill_dry_run",
            cmd=[
                sys.executable,
                "scripts/run_incident_drill.py",
                "--db",
                str(db_path),
                "--workspace",
                ".",
                "--dry-run",
                "--strict-compaction-trace-validation",
            ],
            critical=False,
            timeout_seconds=90,
        )
    )
    checks.append(
        _run_cmd(
            name="recurring_drill_dry_run",
            cmd=[
                sys.executable,
                "scripts/recurring_incident_drill.py",
                "--db",
                str(db_path),
                "--workspace",
                ".",
                "--max-runs",
                "1",
                "--interval-seconds",
                "1",
                "--dry-run",
            ],
            critical=False,
            timeout_seconds=60,
        )
    )
    checks.append(
        _run_cmd(
            name="pytest_events_schema_smoke",
            cmd=[
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests/test_events_schema.py::test_record_event_accepts_existing_valid_type",
                "--maxfail=1",
            ],
            critical=True,
            timeout_seconds=90,
        )
    )
    checks.append(
        _run_cmd(
            name="pytest_reliability_smoke",
            cmd=[
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests/test_reliability_hardening.py",
                "--maxfail=1",
            ],
            critical=True,
            timeout_seconds=90,
        )
    )
    checks.append(
        _run_cmd(
            name="pytest_metrics_smoke",
            cmd=[
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests/test_metrics_exporter.py::test_export_metrics_snapshot_and_prometheus_output",
                "--maxfail=1",
            ],
            critical=True,
            timeout_seconds=90,
        )
    )

    critical_checks = [c for c in checks if c.critical]
    critical_passed = [c for c in critical_checks if c.status == "pass"]
    all_passed = [c for c in checks if c.status == "pass"]
    go_no_go = "GO" if len(critical_passed) == len(critical_checks) else "NO_GO"

    report = {
        "schema_version": "release_readiness_v1",
        "generated_at": _utc_now(),
        "workspace": str(Path.cwd()),
        "go_no_go": go_no_go,
        "summary": {
            "checks_total": len(checks),
            "checks_passed": len(all_passed),
            "critical_total": len(critical_checks),
            "critical_passed": len(critical_passed),
            "critical_failed": len(critical_checks) - len(critical_passed),
            "score": round((len(all_passed) / max(len(checks), 1)) * 100.0, 2),
        },
        "checks": [c.__dict__ for c in checks],
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    print(json.dumps({"go_no_go": go_no_go, "out_json": str(out_path), "summary": report["summary"]}, ensure_ascii=True))
    return 0 if go_no_go == "GO" else 2


if __name__ == "__main__":
    raise SystemExit(main())
