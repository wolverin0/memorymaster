from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from memorymaster.operator import MemoryOperator, OperatorConfig
from memorymaster.service import MemoryService


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_fs_path(path: Path | str) -> str:
    value = str(path)
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _claim_objects(service: MemoryService) -> set[str]:
    claims = service.list_claims(limit=200, include_archived=True)
    return {str(claim.object_value) for claim in claims if claim.object_value}


def _run_shape_case(
    *,
    repo_root: Path,
    workspace: Path,
    case_name: str,
    row: dict[str, Any],
    expected_object: str,
    forbidden_objects: list[str] | None = None,
) -> dict[str, Any]:
    db_path = Path(_normalize_fs_path(workspace / f"{case_name}.db"))
    inbox_path = Path(_normalize_fs_path(workspace / f"{case_name}.jsonl"))
    log_path = Path(_normalize_fs_path(workspace / f"{case_name}_events.jsonl"))

    service = MemoryService(_normalize_fs_path(db_path), workspace_root=Path(_normalize_fs_path(repo_root)))
    service.init_db()
    _write_jsonl(inbox_path, [row])

    operator = MemoryOperator(
        service=service,
        config=OperatorConfig(
            policy_mode="legacy",
            log_jsonl_path=_normalize_fs_path(log_path),
            state_json_path=None,
        ),
    )
    summary = operator.run_stream(inbox_path, poll_seconds=0.05, max_events=1)

    _assert(summary["processed_events"] == 1, f"{case_name}: expected processed_events=1")
    _assert(summary["seen_events"] == 1, f"{case_name}: expected seen_events=1")
    _assert(summary["json_errors"] == 0, f"{case_name}: expected json_errors=0")
    _assert(summary["exit_reason"] == "max_events_reached", f"{case_name}: expected max_events_reached exit")
    _assert(bool(summary["turns"]), f"{case_name}: expected one processed turn")
    _assert(summary["turns"][0]["turn_id"] == row["turn_id"], f"{case_name}: turn_id mismatch")

    objects = _claim_objects(service)
    _assert(expected_object in objects, f"{case_name}: missing expected claim object '{expected_object}'")
    for obj in (forbidden_objects or []):
        _assert(obj not in objects, f"{case_name}: unexpected forbidden object '{obj}' found")

    return {
        "db_path": str(db_path),
        "inbox_path": str(inbox_path),
        "processed_events": summary["processed_events"],
        "seen_events": summary["seen_events"],
        "json_errors": summary["json_errors"],
        "runtime_seconds": summary["runtime_seconds"],
        "claims_count": len(objects),
    }


def _run_resume_case(*, repo_root: Path, workspace: Path) -> dict[str, Any]:
    db_path = Path(_normalize_fs_path(workspace / "resume.db"))
    inbox_path = Path(_normalize_fs_path(workspace / "resume.jsonl"))
    log_path = Path(_normalize_fs_path(workspace / "resume_events.jsonl"))
    state_path = Path(_normalize_fs_path(workspace / "resume_state.json"))

    service = MemoryService(_normalize_fs_path(db_path), workspace_root=Path(_normalize_fs_path(repo_root)))
    service.init_db()
    _write_jsonl(
        inbox_path,
        [
            {
                "session_id": "resume",
                "thread_id": "resume",
                "turn_id": "resume-1",
                "user_text": "Support email is first@example.com",
                "assistant_text": "ok",
                "observations": [],
                "timestamp": "2026-03-02T12:00:00+00:00",
            },
            {
                "session_id": "resume",
                "thread_id": "resume",
                "turn_id": "resume-2",
                "user_text": "Support email is second@example.com",
                "assistant_text": "ok",
                "observations": [],
                "timestamp": "2026-03-02T12:01:00+00:00",
            },
        ],
    )

    config = OperatorConfig(
        policy_mode="legacy",
        log_jsonl_path=_normalize_fs_path(log_path),
        state_json_path=_normalize_fs_path(state_path),
    )

    first = MemoryOperator(service, config=config).run_stream(inbox_path, poll_seconds=0.05, max_events=1)
    second = MemoryOperator(service, config=config).run_stream(inbox_path, poll_seconds=0.05, max_events=1)

    _assert(first["processed_events"] == 1, "resume: first run expected processed_events=1")
    _assert(first["seen_events"] == 1, "resume: first run expected seen_events=1")
    _assert(first["start_offset"] == 0, "resume: first run expected start_offset=0")
    _assert(first["final_offset"] > 0, "resume: first run expected final_offset>0")
    _assert(first["turns"][0]["turn_id"] == "resume-1", "resume: first run expected turn resume-1")

    _assert(second["processed_events"] == 1, "resume: second run expected processed_events=1")
    _assert(second["seen_events"] == 1, "resume: second run expected seen_events=1")
    _assert(second["start_offset"] == first["final_offset"], "resume: checkpoint start_offset mismatch")
    _assert(second["final_offset"] > second["start_offset"], "resume: second run offset did not advance")
    _assert(second["turns"][0]["turn_id"] == "resume-2", "resume: second run expected turn resume-2")

    state = json.loads(state_path.read_text(encoding="utf-8"))
    _assert(state["offset"] == second["final_offset"], "resume: state offset mismatch")
    _assert(state["seen_events"] == 2, "resume: state seen_events mismatch")
    _assert(state["processed_events"] == 2, "resume: state processed_events mismatch")

    objects = _claim_objects(service)
    _assert("first@example.com" in objects, "resume: missing first@example.com claim")
    _assert("second@example.com" in objects, "resume: missing second@example.com claim")

    return {
        "db_path": str(db_path),
        "inbox_path": str(inbox_path),
        "state_path": str(state_path),
        "processed_events": int(first["processed_events"]) + int(second["processed_events"]),
        "seen_events": int(first["seen_events"]) + int(second["seen_events"]),
        "json_errors": int(first["json_errors"]) + int(second["json_errors"]),
        "first_run": first,
        "second_run": second,
    }


def _run_idle_case(*, repo_root: Path, workspace: Path) -> dict[str, Any]:
    db_path = Path(_normalize_fs_path(workspace / "idle.db"))
    inbox_path = Path(_normalize_fs_path(workspace / "idle.jsonl"))

    service = MemoryService(_normalize_fs_path(db_path), workspace_root=Path(_normalize_fs_path(repo_root)))
    service.init_db()
    _write_jsonl(inbox_path, [])

    operator = MemoryOperator(
        service=service,
        config=OperatorConfig(
            policy_mode="legacy",
            max_idle_seconds=0.25,
            log_jsonl_path=None,
            state_json_path=None,
        ),
    )
    summary = operator.run_stream(inbox_path, poll_seconds=0.05)

    _assert(summary["processed_events"] == 0, "idle: expected processed_events=0")
    _assert(summary["seen_events"] == 0, "idle: expected seen_events=0")
    _assert(summary["json_errors"] == 0, "idle: expected json_errors=0")
    _assert(summary["exit_reason"] == "idle_timeout", "idle: expected idle_timeout exit")

    return {
        "db_path": str(db_path),
        "inbox_path": str(inbox_path),
        "processed_events": summary["processed_events"],
        "seen_events": summary["seen_events"],
        "json_errors": summary["json_errors"],
        "runtime_seconds": summary["runtime_seconds"],
        "summary": summary,
    }


def main() -> int:
    repo_root = Path(".")
    tmp_root = repo_root / ".tmp_cases" / "e2e_operator"
    tmp_root.mkdir(parents=True, exist_ok=True)
    fd, marker_raw = tempfile.mkstemp(prefix="run-", dir=_normalize_fs_path(tmp_root))
    os.close(fd)
    marker_path = Path(_normalize_fs_path(marker_raw))
    marker_path.unlink(missing_ok=True)
    workspace = tmp_root / marker_path.name
    workspace.mkdir(parents=True, exist_ok=True)
    report_path = repo_root / "artifacts" / "e2e" / "operator_e2e_report.json"

    checks: list[dict[str, Any]] = []

    def run_check(name: str, fn: Callable[[], dict[str, Any]]) -> None:
        started = time.monotonic()
        try:
            metrics = fn()
            checks.append(
                {
                    "name": name,
                    "passed": True,
                    "details": "ok",
                    "duration_seconds": round(time.monotonic() - started, 4),
                    "metrics": metrics,
                }
            )
        except AssertionError as exc:
            checks.append(
                {
                    "name": name,
                    "passed": False,
                    "details": str(exc) or "assertion failed",
                    "duration_seconds": round(time.monotonic() - started, 4),
                    "metrics": {},
                }
            )
        except Exception as exc:  # pragma: no cover - hard failure path
            checks.append(
                {
                    "name": name,
                    "passed": False,
                    "details": f"{type(exc).__name__}: {exc}",
                    "duration_seconds": round(time.monotonic() - started, 4),
                    "metrics": {},
                }
            )

    run_check(
        "explicit_shape_row",
        lambda: _run_shape_case(
            repo_root=repo_root,
            workspace=workspace,
            case_name="explicit_shape",
            row={
                "session_id": "shape",
                "thread_id": "shape",
                "turn_id": "explicit-1",
                "user_text": "Support email is explicit@example.com",
                "assistant_text": "Noted.",
                "observations": [],
                "timestamp": "2026-03-02T12:00:00+00:00",
            },
            expected_object="explicit@example.com",
        ),
    )
    run_check(
        "events_shape_row",
        lambda: _run_shape_case(
            repo_root=repo_root,
            workspace=workspace,
            case_name="events_shape",
            row={
                "session_id": "shape",
                "thread_id": "shape",
                "turn_id": "events-1",
                "events": [
                    {"role": "user", "text": "Release deadline is 2026-05-01"},
                    {"role": "assistant", "text": "Understood."},
                    {"role": "tool", "text": "auxiliary output"},
                ],
                "timestamp": "2026-03-02T12:01:00+00:00",
            },
            expected_object="2026-05-01",
        ),
    )
    run_check(
        "messages_shape_row",
        lambda: _run_shape_case(
            repo_root=repo_root,
            workspace=workspace,
            case_name="messages_shape",
            row={
                "session_id": "shape",
                "thread_id": "shape",
                "turn_id": "messages-1",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "HQ address is 123 Main St, Springfield"},
                        ],
                    },
                    {"role": "assistant", "content": "Acknowledged."},
                ],
                "timestamp": "2026-03-02T12:02:00+00:00",
            },
            expected_object="123 Main St, Springfield",
        ),
    )
    run_check(
        "private_tag_exclusion",
        lambda: _run_shape_case(
            repo_root=repo_root,
            workspace=workspace,
            case_name="private_tag_exclusion",
            row={
                "session_id": "shape",
                "thread_id": "shape",
                "turn_id": "private-1",
                "user_text": "<private>Support email is hidden@example.com</private> Release deadline is 2026-09-01",
                "assistant_text": "ok",
                "observations": [],
                "timestamp": "2026-03-02T12:03:00+00:00",
            },
            expected_object="2026-09-01",
            forbidden_objects=["hidden@example.com"],
        ),
    )
    run_check("checkpoint_resume_two_runs", lambda: _run_resume_case(repo_root=repo_root, workspace=workspace))
    run_check("idle_timeout_empty_inbox", lambda: _run_idle_case(repo_root=repo_root, workspace=workspace))

    total_checks = len(checks)
    failed = [item for item in checks if not item["passed"]]
    passed = total_checks - len(failed)
    total_seen_events = sum(int(item.get("metrics", {}).get("seen_events", 0)) for item in checks)
    total_processed_events = sum(int(item.get("metrics", {}).get("processed_events", 0)) for item in checks)
    total_json_errors = sum(int(item.get("metrics", {}).get("json_errors", 0)) for item in checks)

    report = {
        "generated_at": _utc_now_iso(),
        "workspace": str(workspace),
        "checks": checks,
        "summary": {
            "status": "pass" if not failed else "fail",
            "total_checks": total_checks,
            "passed_checks": passed,
            "failed_checks": len(failed),
            "total_seen_events": total_seen_events,
            "total_processed_events": total_processed_events,
            "total_json_errors": total_json_errors,
        },
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    if failed:
        print("Operator E2E harness failures:")
        for item in failed:
            print(f"- {item['name']}: {item['details']}")
        print(f"Report: {report_path}")
        return 1

    print(f"Operator E2E harness passed ({passed}/{total_checks})")
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
