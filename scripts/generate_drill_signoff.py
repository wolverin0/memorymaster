from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def load_json_object(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return raw


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_path(raw_path: str, *, workspace_root: Path, run_summary_dir: Path) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    run_relative = run_summary_dir / path
    if run_relative.exists():
        return run_relative
    return workspace_root / path


def _first_non_empty(*values: str) -> str:
    for value in values:
        if to_str(value).strip():
            return to_str(value).strip()
    return ""


def _approval_value(cli_value: str, env_name: str, placeholder: str) -> str:
    value = _first_non_empty(cli_value, to_str(os.environ.get(env_name)))
    return value if value else placeholder


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a drill signoff artifact with checksums and approval fields.")
    parser.add_argument(
        "--run-summary",
        required=True,
        help="Path to incident_drill_run.json.",
    )
    parser.add_argument(
        "--workspace-root",
        default=".",
        help="Workspace root used to resolve relative artifact paths.",
    )
    parser.add_argument(
        "--evidence-md",
        default="",
        help="Optional explicit path to incident_drill_evidence.md.",
    )
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="Additional artifact path to include in checksums (repeatable).",
    )
    parser.add_argument(
        "--out-json",
        default="",
        help="Output signoff JSON path. Defaults to <run-summary-dir>/signoff/drill_signoff.json.",
    )
    parser.add_argument("--approver-name", default="", help="Approver name override.")
    parser.add_argument("--approver-email", default="", help="Approver email override.")
    parser.add_argument("--approver-role", default="", help="Approver role override.")
    parser.add_argument("--decision", default="", help="Approval decision (for example: approved/rejected/pending).")
    parser.add_argument("--approval-ticket", default="", help="Ticket/change request id for the signoff.")
    parser.add_argument("--approval-notes", default="", help="Optional notes for signoff.")
    parser.add_argument(
        "--signing-key",
        default="",
        help="Optional explicit signing key. Prefer --signing-key-env in automation.",
    )
    parser.add_argument(
        "--signing-key-env",
        default="MEMORYMASTER_DRILL_SIGNING_KEY",
        help="Environment variable containing the signing key.",
    )
    parser.add_argument(
        "--strict-missing",
        action="store_true",
        help="Exit non-zero if any referenced artifact file is missing.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_summary_path = Path(args.run_summary)
    if not run_summary_path.exists():
        print(f"error: missing run summary: {run_summary_path}")
        return 2

    workspace_root = Path(args.workspace_root)
    run_summary = load_json_object(run_summary_path)
    run_summary_dir = run_summary_path.parent
    drill_id = to_str(run_summary.get("drill_id")).strip()
    run_status = to_str(run_summary.get("status")).strip()

    default_out = run_summary_dir / "signoff" / "drill_signoff.json"
    out_json = Path(args.out_json) if to_str(args.out_json).strip() else default_out
    if not out_json.is_absolute():
        out_json = run_summary_dir / out_json

    artifacts: list[str] = []
    seen: set[str] = set()

    def _add_path(raw: str) -> None:
        if not to_str(raw).strip():
            return
        key = to_str(raw).strip()
        if key in seen:
            return
        seen.add(key)
        artifacts.append(key)

    _add_path(str(run_summary_path))

    evidence_hint = _first_non_empty(
        to_str(args.evidence_md),
        to_str(run_summary.get("artifacts", {}).get("incident_drill_evidence_md"))
        if isinstance(run_summary.get("artifacts"), dict)
        else "",
    )
    _add_path(evidence_hint)

    artifacts_map = run_summary.get("artifacts")
    if isinstance(artifacts_map, dict):
        for value in artifacts_map.values():
            if isinstance(value, str):
                _add_path(value)

    for extra in args.artifact:
        _add_path(extra)

    file_rows: list[dict[str, Any]] = []
    missing_files: list[str] = []
    for raw in artifacts:
        resolved = _resolve_path(raw, workspace_root=workspace_root, run_summary_dir=run_summary_dir)
        exists = resolved.exists() and resolved.is_file()
        row: dict[str, Any] = {
            "path": raw,
            "resolved_path": str(resolved),
            "exists": exists,
            "size_bytes": 0,
            "sha256": "",
        }
        if exists:
            row["size_bytes"] = int(resolved.stat().st_size)
            row["sha256"] = sha256_file(resolved)
        else:
            missing_files.append(raw)
        file_rows.append(row)

    approver = {
        "name": _approval_value(args.approver_name, "MEMORYMASTER_DRILL_APPROVER_NAME", "TBD"),
        "email": _approval_value(args.approver_email, "MEMORYMASTER_DRILL_APPROVER_EMAIL", "TBD"),
        "role": _approval_value(args.approver_role, "MEMORYMASTER_DRILL_APPROVER_ROLE", "TBD"),
        "decision": _approval_value(args.decision, "MEMORYMASTER_DRILL_APPROVER_DECISION", "pending"),
        "ticket": _approval_value(args.approval_ticket, "MEMORYMASTER_DRILL_APPROVAL_TICKET", "TBD"),
        "notes": _first_non_empty(args.approval_notes, to_str(os.environ.get("MEMORYMASTER_DRILL_APPROVER_NOTES"))),
        "signed_at": utc_now(),
    }

    missing_approval_fields: list[str] = []
    for key in ("name", "email", "role", "ticket"):
        if to_str(approver.get(key)).strip() in {"", "TBD"}:
            missing_approval_fields.append(key)
    if to_str(approver.get("decision")).strip().lower() in {"", "tbd"}:
        missing_approval_fields.append("decision")

    files_present = sum(1 for row in file_rows if bool(row.get("exists")))
    files_missing = len(file_rows) - files_present

    signature_payload = {
        "drill_id": drill_id,
        "run_status": run_status,
        "files": [{"path": row["path"], "sha256": row["sha256"], "exists": row["exists"]} for row in file_rows],
        "approver": {
            "name": approver["name"],
            "email": approver["email"],
            "role": approver["role"],
            "decision": approver["decision"],
            "ticket": approver["ticket"],
            "signed_at": approver["signed_at"],
        },
    }
    canonical = json.dumps(signature_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    payload_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    signing_key = _first_non_empty(args.signing_key, to_str(os.environ.get(to_str(args.signing_key_env))))
    if signing_key:
        algorithm = "hmac-sha256"
        signature = hmac.new(signing_key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        key_source = f"env:{args.signing_key_env}" if not to_str(args.signing_key).strip() else "arg:--signing-key"
        signed = True
    else:
        algorithm = "sha256"
        signature = payload_sha256
        key_source = "none"
        signed = False

    signoff_complete = (files_missing == 0) and (len(missing_approval_fields) == 0)
    status = "complete" if signoff_complete else "pending"
    if args.strict_missing and files_missing > 0:
        status = "fail"

    report = {
        "schema_version": "1.0",
        "artifact_type": "drill_signoff",
        "generated_at": utc_now(),
        "status": status,
        "drill_id": drill_id,
        "run_status": run_status,
        "run_summary_json": str(run_summary_path),
        "approver": approver,
        "approval": {
            "complete": signoff_complete,
            "missing_fields": missing_approval_fields,
        },
        "files": file_rows,
        "totals": {
            "files": len(file_rows),
            "files_present": files_present,
            "files_missing": files_missing,
        },
        "signature": {
            "algorithm": algorithm,
            "signed": signed,
            "key_source": key_source,
            "payload_sha256": payload_sha256,
            "signature": signature,
        },
    }
    write_json(out_json, report)
    print(f"status={status} wrote={out_json}")

    if args.strict_missing and files_missing > 0:
        missing = ", ".join(missing_files)
        print(f"error: missing artifacts: {missing}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
