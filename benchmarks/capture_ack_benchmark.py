"""Deterministic public-capture acknowledgement and integrity benchmark.

Uses a temporary SQLite database and a fake claim extractor. It measures the
synchronous ``remember`` path before draining queued work, then proves replay,
lineage, secret-redaction, and terminal-state invariants without external
providers or live data.
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memorymaster import remember  # noqa: E402
from memorymaster.capture import CaptureRejected  # noqa: E402
from memorymaster.capture.worker import run_capture_worker  # noqa: E402
from memorymaster.core.service import MemoryService  # noqa: E402


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return round(ordered[index], 6)


def _timed(call: Callable[[], Any]) -> tuple[Any, float]:
    started = time.perf_counter()
    result = call()
    return result, (time.perf_counter() - started) * 1000


def _count(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(query, params).fetchone()
    return int(row[0]) if row else 0


def _duplicate_jobs(conn: sqlite3.Connection) -> int:
    return _count(
        conn,
        """SELECT COALESCE(SUM(c - 1), 0) FROM (
               SELECT COUNT(*) c FROM capture_jobs
               GROUP BY source_item_id, content_hash, stage HAVING COUNT(*) > 1
           )""",
    )


def _integrity_counts(db: Path, secret: str) -> dict[str, int]:
    with sqlite3.connect(db) as conn:
        return {
            "duplicate_jobs": _duplicate_jobs(conn),
            "orphan_jobs": _count(
                conn,
                """SELECT COUNT(*) FROM capture_jobs j LEFT JOIN source_items s
                   ON s.id=j.source_item_id WHERE s.id IS NULL""",
            ),
            "orphan_evidence": _count(
                conn,
                """SELECT COUNT(*) FROM evidence_items e LEFT JOIN source_items s
                   ON s.id=e.source_item_id WHERE s.id IS NULL""",
            ),
            "nonterminal_jobs": _count(
                conn,
                """SELECT COUNT(*) FROM capture_jobs
                   WHERE status NOT IN ('completed','retryable','blocked')""",
            ),
            "leased_jobs": _count(
                conn, "SELECT COUNT(*) FROM capture_jobs WHERE status='leased'"
            ),
            "leaked_secret_rows": _count(
                conn,
                """SELECT COUNT(*) FROM source_items
                   WHERE COALESCE(text, '') LIKE ? OR COALESCE(payload_json, '') LIKE ?""",
                (f"%{secret}%", f"%{secret}%"),
            )
            + _count(
                conn,
                """SELECT COUNT(*) FROM evidence_items
                   WHERE COALESCE(text, '') LIKE ? OR COALESCE(payload_json, '') LIKE ?""",
                (f"%{secret}%", f"%{secret}%"),
            ),
            "jobs": _count(conn, "SELECT COUNT(*) FROM capture_jobs"),
            "completed_jobs": _count(
                conn, "SELECT COUNT(*) FROM capture_jobs WHERE status='completed'"
            ),
            "blocked_jobs": _count(
                conn, "SELECT COUNT(*) FROM capture_jobs WHERE status='blocked'"
            ),
        }


def _drain(db: Path, workspace: Path, limit: int) -> tuple[int, int]:
    service = MemoryService(str(db), workspace_root=workspace)
    service.init_db()
    fake_result = SimpleNamespace(degraded=0, partial=0, invalid_rows=0)
    with patch(
        "memorymaster.bridges.atlas_llm_extractor.extract_atlas_claims_llm",
        return_value=fake_result,
    ) as extractor:
        result = run_capture_worker(service, owner="capture-benchmark", limit=limit)
    return result.completed, extractor.call_count


def _reject_secret_url(db: Path, workspace: Path, secret: str) -> int:
    try:
        remember(
            source_uri=f"https://example.com/?api_key={secret}",
            db=db,
            workspace=workspace,
        )
    except CaptureRejected:
        return 1
    return 0


def _capture_inputs(
    db: Path, workspace: Path, secret: str, text_count: int, url_count: int
) -> dict[str, Any]:
    texts = tuple(f"Capture benchmark fact {index}." for index in range(text_count))
    text_runs = tuple(
        _timed(lambda value=value: remember(text=value, db=db, workspace=workspace))
        for value in texts
    )
    replay_runs = tuple(
        _timed(lambda value=value: remember(text=value, db=db, workspace=workspace))
        for value in texts
    )
    url_runs = tuple(
        _timed(
            lambda index=index: remember(
                source_uri=f"https://example.com/reference/{index}",
                db=db,
                workspace=workspace,
            )
        )
        for index in range(url_count)
    )
    update_uri = "https://example.com/updated"
    updates = tuple(
        remember(text=value, source_uri=update_uri, db=db, workspace=workspace)
        for value in ("Version one.", "Version two.")
    )
    secret_receipt = remember(text=f"note token={secret}", db=db, workspace=workspace)
    return {
        "text_runs": text_runs,
        "replay_runs": replay_runs,
        "url_runs": url_runs,
        "updates": updates,
        "secret_receipt": secret_receipt,
        "secret_url_rejected": _reject_secret_url(db, workspace, secret),
    }


def _summarize(
    captured: dict[str, Any], counts: dict[str, int], completed: int, calls: int
) -> dict[str, Any]:
    text_ms = [elapsed for _, elapsed in captured["text_runs"]]
    replay_ms = [elapsed for _, elapsed in captured["replay_runs"]]
    url_ms = [elapsed for _, elapsed in captured["url_runs"]]
    replays = [receipt for receipt, _ in captured["replay_runs"]]
    update_jobs = {job for receipt in captured["updates"] for job in receipt.job_ids}
    secret_receipt = captured["secret_receipt"]
    return {
        "ack_p95_ms": max(_p95(text_ms), _p95(url_ms)),
        "text_ack_p95_ms": _p95(text_ms),
        "reference_ack_p95_ms": _p95(url_ms),
        "replay_ack_p95_ms": _p95(replay_ms),
        "accepted_calls": len(text_ms) * 2 + len(url_ms) + 3,
        "text_samples": len(text_ms),
        "reference_samples": len(url_ms),
        "replay_failures": sum(not receipt.deduplicated for receipt in replays),
        "update_job_delta": len(update_jobs),
        "secret_warning": int("sensitive_content_redacted" in secret_receipt.warnings),
        "secret_url_rejected": captured["secret_url_rejected"],
        "provider_calls": 0,
        "fake_extractor_calls": calls,
        "worker_completed": completed,
        "elapsed_sample_ms": round(sum((*text_ms, *url_ms)), 6),
        **counts,
    }


def run_benchmark(*, text_count: int = 40, url_count: int = 10) -> dict[str, Any]:
    secret = "capture-benchmark-super-secret-value"
    with tempfile.TemporaryDirectory(
        prefix="memorymaster-capture-benchmark-", ignore_cleanup_errors=True
    ) as raw:
        workspace = Path(raw) / "workspace"
        workspace.mkdir()
        db = Path(raw) / "capture.db"
        env = {
            "MEMORYMASTER_CAPTURE_ROOTS": f"benchmark={workspace}",
            "MEMORYMASTER_CAPTURE_TRUST_MODE": "local-trusted",
        }
        with patch.dict(os.environ, env, clear=False):
            captured = _capture_inputs(db, workspace, secret, text_count, url_count)
            completed, calls = _drain(db, workspace, text_count + url_count + 3)
        result = _summarize(captured, _integrity_counts(db, secret), completed, calls)
        del captured
        gc.collect()
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--text-count", type=int, default=40)
    parser.add_argument("--url-count", type=int, default=10)
    args = parser.parse_args()
    if args.text_count <= 0 or args.url_count <= 0:
        parser.error("sample counts must be greater than zero")
    result = run_benchmark(text_count=args.text_count, url_count=args.url_count)
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
