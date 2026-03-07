from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def _notify_webhook(url: str, payload: dict[str, Any], timeout_seconds: float) -> tuple[bool, str]:
    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    request = urllib.request.Request(
        url=url,
        method="POST",
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "memorymaster-recurring-drill"},
    )
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, timeout_seconds)) as response:
            status = int(getattr(response, "status", 200))
        if 200 <= status < 300:
            return True, f"http_status={status}"
        return False, f"http_status={status}"
    except urllib.error.URLError as exc:
        return False, f"url_error={exc}"
    except Exception as exc:  # pragma: no cover
        return False, f"error={exc}"


def _drill_id(prefix: str, sequence: int) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{ts}-{sequence:04d}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run incident drills on a schedule with optional failure alerts.")
    parser.add_argument("--db", default="memorymaster.db", help="SQLite path or Postgres DSN.")
    parser.add_argument("--workspace", default=".", help="Workspace root for deterministic checks.")
    parser.add_argument("--artifacts-root", default="artifacts/incident_drill", help="Drill artifacts root.")
    parser.add_argument("--python-executable", default=sys.executable, help="Python executable for child drill runs.")
    parser.add_argument("--interval-seconds", type=float, default=3600.0, help="Delay between runs.")
    parser.add_argument("--max-runs", type=int, default=1, help="Run count (set <=0 for unbounded).")
    parser.add_argument("--drill-id-prefix", default="scheduled-drill", help="Prefix for generated drill ids.")
    parser.add_argument("--notify-webhook-url", default="", help="POST this URL on failing drill runs.")
    parser.add_argument("--notify-webhook-env", default="", help="Environment variable containing webhook URL.")
    parser.add_argument("--notify-timeout-seconds", type=float, default=5.0, help="Webhook timeout in seconds.")
    parser.add_argument("--stop-on-fail", action="store_true", help="Stop scheduler after the first failed run.")
    parser.add_argument("--skip-compaction-trace-validation", action="store_true", help="Forwarded to drill runner.")
    parser.add_argument("--strict-compaction-trace-validation", action="store_true", help="Forwarded to drill runner.")
    parser.add_argument("--skip-drill-signoff", action="store_true", help="Forwarded to drill runner.")
    parser.add_argument("--strict-drill-signoff-missing", action="store_true", help="Forwarded to drill runner.")
    parser.add_argument("--strict-drill-signoff-complete", action="store_true", help="Forwarded to drill runner.")
    parser.add_argument("--strict-drill-signoff-signed", action="store_true", help="Forwarded to drill runner.")
    parser.add_argument("--approver-name", default="", help="Forwarded drill signoff approver name.")
    parser.add_argument("--approver-email", default="", help="Forwarded drill signoff approver email.")
    parser.add_argument("--approver-role", default="", help="Forwarded drill signoff approver role.")
    parser.add_argument("--approver-decision", default="", help="Forwarded drill signoff decision.")
    parser.add_argument("--approval-ticket", default="", help="Forwarded drill signoff ticket/change id.")
    parser.add_argument("--approval-notes", default="", help="Forwarded drill signoff notes.")
    parser.add_argument(
        "--signing-key-env",
        default="MEMORYMASTER_DRILL_SIGNING_KEY",
        help="Environment variable name passed to drill signoff generator.",
    )
    parser.add_argument("--run-args", default="", help="Extra args forwarded to scripts/run_incident_drill.py")
    parser.add_argument("--dry-run", action="store_true", help="Print planned command and exit.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    artifacts_root = Path(args.artifacts_root)
    history_jsonl = artifacts_root / "recurring_history.jsonl"
    latest_json = artifacts_root / "recurring_latest.json"

    webhook = _to_str(args.notify_webhook_url).strip()
    if not webhook:
        env_name = _to_str(args.notify_webhook_env).strip()
        if env_name:
            webhook = _to_str(os.environ.get(env_name)).strip()

    forwarded = [part for part in _to_str(args.run_args).split(" ") if part.strip()]
    base_cmd = [
        str(args.python_executable),
        "scripts/run_incident_drill.py",
        "--db",
        str(args.db),
        "--workspace",
        str(args.workspace),
        "--artifacts-root",
        str(args.artifacts_root),
        *forwarded,
    ]
    if args.skip_compaction_trace_validation:
        base_cmd.append("--skip-compaction-trace-validation")
    if args.strict_compaction_trace_validation:
        base_cmd.append("--strict-compaction-trace-validation")
    if args.skip_drill_signoff:
        base_cmd.append("--skip-drill-signoff")
    if args.strict_drill_signoff_missing:
        base_cmd.append("--strict-drill-signoff-missing")
    if args.strict_drill_signoff_complete:
        base_cmd.append("--strict-drill-signoff-complete")
    if args.strict_drill_signoff_signed:
        base_cmd.append("--strict-drill-signoff-signed")
    if _to_str(args.signing_key_env).strip():
        base_cmd.extend(["--signing-key-env", _to_str(args.signing_key_env).strip()])
    if _to_str(args.approver_name).strip():
        base_cmd.extend(["--approver-name", _to_str(args.approver_name).strip()])
    if _to_str(args.approver_email).strip():
        base_cmd.extend(["--approver-email", _to_str(args.approver_email).strip()])
    if _to_str(args.approver_role).strip():
        base_cmd.extend(["--approver-role", _to_str(args.approver_role).strip()])
    if _to_str(args.approver_decision).strip():
        base_cmd.extend(["--approver-decision", _to_str(args.approver_decision).strip()])
    if _to_str(args.approval_ticket).strip():
        base_cmd.extend(["--approval-ticket", _to_str(args.approval_ticket).strip()])
    if _to_str(args.approval_notes).strip():
        base_cmd.extend(["--approval-notes", _to_str(args.approval_notes).strip()])

    if args.dry_run:
        payload = {
            "timestamp": utc_now(),
            "status": "dry_run",
            "command_template": base_cmd,
            "interval_seconds": float(args.interval_seconds),
            "max_runs": int(args.max_runs),
            "stop_on_fail": bool(args.stop_on_fail),
            "webhook_configured": bool(webhook),
        }
        print(json.dumps(payload, indent=2))
        return 0

    run_index = 0
    failure_count = 0
    while True:
        run_index += 1
        if args.max_runs > 0 and run_index > args.max_runs:
            break

        drill_id = _drill_id(_to_str(args.drill_id_prefix).strip() or "scheduled-drill", run_index)
        cmd = [*base_cmd, "--drill-id", drill_id]
        started = time.monotonic()
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        duration_seconds = round(time.monotonic() - started, 3)

        summary_path = artifacts_root / drill_id / "incident_drill_run.json"
        summary = _read_json(summary_path)
        status = _to_str(summary.get("status")).strip().lower()
        if not status:
            status = "pass" if proc.returncode == 0 else "fail"
        gates = summary.get("gates") if isinstance(summary.get("gates"), dict) else {}
        automation = summary.get("automation") if isinstance(summary.get("automation"), dict) else {}
        signoff_data = automation.get("drill_signoff") if isinstance(automation.get("drill_signoff"), dict) else {}
        signoff_status = _to_str(signoff_data.get("status")).strip().lower()
        if not signoff_status and args.skip_drill_signoff:
            signoff_status = "skipped"
        signoff_complete = bool(signoff_data.get("approval_complete")) if signoff_data else False
        if not signoff_complete:
            signoff_complete = bool(gates.get("signoff_complete"))
        signoff_signed = bool(signoff_data.get("signed")) if signoff_data else False
        if not signoff_signed:
            signoff_signed = bool(gates.get("signoff_signed"))
        signoff_signature_algorithm = _to_str(signoff_data.get("signature_algorithm")).strip().lower()
        require_signoff_complete = bool(gates.get("require_signoff_complete"))
        require_signoff_signed = bool(gates.get("require_signoff_signed"))
        signoff_complete_ok = bool(gates.get("signoff_complete_ok")) if ("signoff_complete_ok" in gates) else (
            (not require_signoff_complete) or signoff_complete
        )
        signoff_signed_ok = bool(gates.get("signoff_signed_ok")) if ("signoff_signed_ok" in gates) else (
            (not require_signoff_signed) or signoff_signed
        )
        signoff_report_json = ""
        artifacts = summary.get("artifacts")
        if isinstance(artifacts, dict):
            signoff_report_json = _to_str(artifacts.get("drill_signoff_json")).strip()
        is_fail = status != "pass"
        if is_fail:
            failure_count += 1

        row = {
            "timestamp": utc_now(),
            "run_index": run_index,
            "drill_id": drill_id,
            "status": status,
            "returncode": int(proc.returncode),
            "duration_seconds": duration_seconds,
            "summary_json": str(summary_path),
            "stdout_tail": (_to_str(proc.stdout)[-800:] if proc.stdout else ""),
            "stderr_tail": (_to_str(proc.stderr)[-800:] if proc.stderr else ""),
            "gates": gates,
            "signoff_status": signoff_status,
            "signoff_complete": signoff_complete,
            "signoff_signed": signoff_signed,
            "signoff_signature_algorithm": signoff_signature_algorithm,
            "require_signoff_complete": require_signoff_complete,
            "require_signoff_signed": require_signoff_signed,
            "signoff_complete_ok": signoff_complete_ok,
            "signoff_signed_ok": signoff_signed_ok,
            "signoff_report_json": signoff_report_json,
        }
        _append_jsonl(history_jsonl, row)
        _write_json(
            latest_json,
            {
                "timestamp": utc_now(),
                "latest": row,
                "totals": {"runs": run_index, "failures": failure_count},
            },
        )

        if is_fail and webhook:
            notify_payload = {
                "kind": "memorymaster.incident_drill.failure",
                "timestamp": utc_now(),
                "drill_id": drill_id,
                "status": status,
                "returncode": int(proc.returncode),
                "summary_json": str(summary_path),
                "gates": row["gates"],
                "signoff_status": signoff_status,
                "signoff_complete": signoff_complete,
                "signoff_signed": signoff_signed,
                "signoff_signature_algorithm": signoff_signature_algorithm,
                "require_signoff_complete": require_signoff_complete,
                "require_signoff_signed": require_signoff_signed,
                "signoff_complete_ok": signoff_complete_ok,
                "signoff_signed_ok": signoff_signed_ok,
                "signoff_report_json": signoff_report_json,
            }
            ok, detail = _notify_webhook(webhook, notify_payload, timeout_seconds=float(args.notify_timeout_seconds))
            _append_jsonl(
                history_jsonl,
                {
                    "timestamp": utc_now(),
                    "run_index": run_index,
                    "drill_id": drill_id,
                    "event": "webhook_notify",
                    "ok": ok,
                    "detail": detail,
                },
            )

        print(json.dumps(row, ensure_ascii=True))

        if is_fail and args.stop_on_fail:
            break
        if args.max_runs > 0 and run_index >= args.max_runs:
            break
        time.sleep(max(0.1, float(args.interval_seconds)))

    result = {
        "timestamp": utc_now(),
        "runs": run_index,
        "failures": failure_count,
        "history_jsonl": str(history_jsonl),
        "latest_json": str(latest_json),
    }
    print(json.dumps(result, indent=2))
    return 0 if failure_count == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
