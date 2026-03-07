from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

from memorymaster.models import CitationInput
from memorymaster.service import MemoryService

OLD_TS = "2025-01-01T00:00:00+00:00"


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"Expected JSON object in {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _force_claim_state(db_path: Path, claim_id: int, *, status: str, updated_at: str = OLD_TS) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "UPDATE claims SET status = ?, updated_at = ?, last_validated_at = ? WHERE id = ?",
            (status, updated_at, updated_at, claim_id),
        )
        con.commit()
    finally:
        con.close()


def _citation(source: str, locator: str, excerpt: str) -> CitationInput:
    return CitationInput(source=source, locator=locator, excerpt=excerpt)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _run_validator(repo_root: Path, artifacts_dir: Path, out_json: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        str(repo_root / "scripts" / "compaction_trace_validate.py"),
        "--artifacts-dir",
        str(artifacts_dir),
        "--out-json",
        str(out_json),
    ]
    proc = subprocess.run(command, cwd=str(repo_root), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        std = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        raise AssertionError(
            "validator returned non-zero exit code "
            f"{proc.returncode}. stdout='{std}' stderr='{err}'"
        )
    report = _read_json(out_json)
    _assert(bool(report.get("passed", False)), f"validator report failed: {out_json}")
    return report


def _run_case_conflicted_lineage(case_dir: Path, repo_root: Path) -> dict[str, Any]:
    db_path = case_dir / "case.db"
    workspace = case_dir / "workspace"
    service = MemoryService(db_path, workspace_root=workspace)
    service.init_db()

    stale = service.ingest(
        text="Legacy endpoint is https://old.service.example/v1",
        citations=[
            _citation("session://chat", "turn-edge-1", "legacy endpoint mention"),
            _citation("ticket://ops", "INC-2042", "cutover not completed"),
        ],
        subject="service",
        predicate="endpoint",
        object_value="https://old.service.example/v1",
    )
    conflicted = service.ingest(
        text="Incorrect endpoint is https://mistyped.service.example/v1",
        citations=[_citation("session://chat", "turn-edge-2", "conflicting endpoint evidence")],
        subject="service",
        predicate="endpoint",
        object_value="https://mistyped.service.example/v1",
    )
    superseded = service.ingest(
        text="Release date was 2026-04-01",
        citations=[_citation("session://chat", "turn-edge-3", "old schedule decision")],
        subject="release",
        predicate="deadline",
        object_value="2026-04-01",
    )
    confirmed = service.ingest(
        text="Current endpoint is https://api.service.example/v2",
        citations=[_citation("session://chat", "turn-edge-4", "current endpoint")],
        subject="service",
        predicate="endpoint",
        object_value="https://api.service.example/v2",
    )

    _force_claim_state(db_path, stale.id, status="stale")
    _force_claim_state(db_path, conflicted.id, status="conflicted")
    _force_claim_state(db_path, superseded.id, status="superseded")
    _force_claim_state(db_path, confirmed.id, status="confirmed")

    compact_result = service.compact(retain_days=30, event_retain_days=36500)
    _assert(compact_result == {"archived_claims": 3, "deleted_events": 0}, "unexpected compact result for edge case")

    stale_after = service.store.get_claim(stale.id, include_citations=False)
    conflicted_after = service.store.get_claim(conflicted.id, include_citations=False)
    superseded_after = service.store.get_claim(superseded.id, include_citations=False)
    confirmed_after = service.store.get_claim(confirmed.id, include_citations=False)
    _assert(stale_after is not None and stale_after.status == "archived", "stale claim should be archived")
    _assert(conflicted_after is not None and conflicted_after.status == "archived", "conflicted claim should be archived")
    _assert(superseded_after is not None and superseded_after.status == "archived", "superseded claim should be archived")
    _assert(confirmed_after is not None and confirmed_after.status == "confirmed", "confirmed claim should remain confirmed")

    artifacts_dir = workspace / "artifacts" / "compaction"
    summary_graph = _read_json(artifacts_dir / "summary_graph.json")
    traceability = _read_json(artifacts_dir / "traceability.json")

    claim_nodes = summary_graph.get("nodes", {}).get("claims", [])
    citation_nodes = summary_graph.get("nodes", {}).get("citations", [])
    _assert(len(claim_nodes) == 3, "expected 3 archived claim nodes")
    _assert(len(citation_nodes) == 4, "expected 4 retained citation nodes")

    lineage = {
        int(row["claim_id"]): row
        for row in traceability.get("claim_lineage", [])
        if isinstance(row, dict) and row.get("claim_id") is not None
    }
    _assert(set(lineage.keys()) == {stale.id, conflicted.id, superseded.id}, "missing claim lineage rows")
    _assert(lineage[conflicted.id].get("status_before") == "conflicted", "conflicted status_before must be retained")
    conflicted_citations = lineage[conflicted.id].get("citations", [])
    _assert(len(conflicted_citations) == 1, "conflicted claim should retain exactly one source citation")
    _assert(conflicted_citations[0].get("locator") == "turn-edge-2", "conflicted citation locator mismatch")

    endpoint_summary_row = None
    for row in traceability.get("summary_to_source", []):
        if not isinstance(row, dict):
            continue
        claim_ids = {int(cid) for cid in row.get("claim_ids", [])}
        if claim_ids == {stale.id, conflicted.id}:
            endpoint_summary_row = row
            break
    _assert(endpoint_summary_row is not None, "expected endpoint summary row with stale+conflicted claims")
    _assert(
        len(endpoint_summary_row.get("source_citations", [])) == 3,
        "endpoint summary row should retain three source citations",
    )

    compaction_events = service.list_events(event_type="compaction_run", limit=1)
    _assert(bool(compaction_events), "missing compaction_run event")
    event_payload = json.loads(compaction_events[0].payload_json or "{}")
    _assert(event_payload.get("archived_claims") == 3, "compaction_run payload archived_claims mismatch")
    artifacts_payload = event_payload.get("artifacts", {})
    _assert(str(artifacts_payload.get("summary_graph", "")).endswith("summary_graph.json"), "summary_graph path missing")
    _assert(str(artifacts_payload.get("traceability", "")).endswith("traceability.json"), "traceability path missing")

    validator_report_path = artifacts_dir / "edge_validator_report.json"
    validator_report = _run_validator(repo_root, artifacts_dir, validator_report_path)

    return {
        "archived_claims": compact_result["archived_claims"],
        "validator_report": str(validator_report_path),
        "validator_checks_failed": int(validator_report.get("metrics", {}).get("checks_failed", 0)),
        "artifacts_dir": str(artifacts_dir),
    }


def _run_case_empty_candidates(case_dir: Path, repo_root: Path) -> dict[str, Any]:
    db_path = case_dir / "case.db"
    workspace = case_dir / "workspace"
    service = MemoryService(db_path, workspace_root=workspace)
    service.init_db()

    confirmed = service.ingest(
        text="Support email is help@service.example",
        citations=[_citation("session://chat", "turn-empty-1", "active support inbox")],
        subject="support",
        predicate="email",
        object_value="help@service.example",
    )
    _force_claim_state(db_path, confirmed.id, status="confirmed")

    compact_result = service.compact(retain_days=30, event_retain_days=36500)
    _assert(compact_result == {"archived_claims": 0, "deleted_events": 0}, "empty case should archive nothing")

    artifacts_dir = workspace / "artifacts" / "compaction"
    summary_graph = _read_json(artifacts_dir / "summary_graph.json")
    traceability = _read_json(artifacts_dir / "traceability.json")
    _assert(summary_graph.get("nodes", {}).get("claims", []) == [], "empty case claims node list must be empty")
    _assert(summary_graph.get("nodes", {}).get("citations", []) == [], "empty case citation node list must be empty")
    _assert(traceability.get("claim_lineage", []) == [], "empty case claim_lineage must be empty")
    _assert(traceability.get("summary_to_source", []) == [], "empty case summary_to_source must be empty")

    validator_report_path = artifacts_dir / "edge_validator_report.json"
    validator_report = _run_validator(repo_root, artifacts_dir, validator_report_path)

    return {
        "archived_claims": compact_result["archived_claims"],
        "validator_report": str(validator_report_path),
        "validator_checks_failed": int(validator_report.get("metrics", {}).get("checks_failed", 0)),
        "artifacts_dir": str(artifacts_dir),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run bounded compaction edge-case validations for CI gating.")
    parser.add_argument("--workspace", default=".", help="Repository/workspace root.")
    parser.add_argument(
        "--tmp-root",
        default=".tmp_cases/compaction_edge_cases",
        help="Case working directory root (workspace-relative unless absolute).",
    )
    parser.add_argument(
        "--out-json",
        default="artifacts/eval/compaction_edge_cases_report.json",
        help="Report JSON path (workspace-relative unless absolute).",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Do not delete existing tmp root before running cases.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(args.workspace).resolve()
    tmp_root = Path(args.tmp_root)
    if not tmp_root.is_absolute():
        tmp_root = repo_root / tmp_root
    out_json = Path(args.out_json)
    if not out_json.is_absolute():
        out_json = repo_root / out_json

    if tmp_root.exists() and not args.keep_temp:
        shutil.rmtree(tmp_root)
    tmp_root.mkdir(parents=True, exist_ok=True)

    cases = [
        ("conflicted_lineage_retention", _run_case_conflicted_lineage),
        ("empty_candidates_emit_valid_artifacts", _run_case_empty_candidates),
    ]

    results: list[dict[str, Any]] = []
    failed = False
    for case_name, case_fn in cases:
        case_dir = tmp_root / case_name
        case_dir.mkdir(parents=True, exist_ok=True)
        print(f"[compaction-edge] running case={case_name}")
        try:
            details = case_fn(case_dir, repo_root)
            results.append({"case": case_name, "passed": True, "details": details})
            print(f"[compaction-edge] pass case={case_name}")
        except Exception as exc:  # pragma: no cover - failure path exercised in CI only
            failed = True
            results.append(
                {
                    "case": case_name,
                    "passed": False,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )
            print(f"[compaction-edge] fail case={case_name} error={exc}")

    report = {
        "status": "pass" if not failed else "fail",
        "passed": not failed,
        "cases_total": len(cases),
        "cases_failed": sum(1 for row in results if not row.get("passed", False)),
        "results": results,
    }
    _write_json(out_json, report)
    print(f"[compaction-edge] wrote report={out_json}")
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
