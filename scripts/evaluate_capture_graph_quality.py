"""Run the private governed capture/graph precision gate on disposable SQLite.

The fixture must contain only synthetic JSONL cases and remain outside the
repository. Results contain aggregate counts and case IDs, never source text.
"""

from __future__ import annotations

import argparse
import contextlib
import gc
import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memorymaster import improve, remember
from memorymaster.capture.worker import run_capture_worker
from memorymaster.core import llm_provider
from memorymaster.core.service import MemoryService

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = (
    Path.home() / ".memorymaster" / "evals" / "vnext-governed-capture-v1" / "eval.jsonl"
)
TARGETS = {"candidate": 0.90, "entity": 0.90, "relationship": 0.85}
_NORMALIZE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class Counts:
    correct: int
    predicted: int
    expected: int


def _norm(value: Any) -> str:
    return _NORMALIZE.sub(" ", str(value or "").casefold()).strip()


def _claim_key(row: Any) -> tuple[str, str, str]:
    getter = row.get if isinstance(row, dict) else lambda name, default="": getattr(row, name, default)
    return (
        _norm(getter("subject", "")),
        _norm(getter("predicate", "")),
        _norm(getter("object_value", getter("object", ""))),
    )


def _typed_claim_key(row: Any) -> tuple[str, str, str, str]:
    getter = row.get if isinstance(row, dict) else lambda name, default="": getattr(row, name, default)
    return (
        _norm(getter("claim_type", getter("type", ""))),
        *_claim_key(row),
    )


def _relation_key(row: Any) -> tuple[str, str, str]:
    if isinstance(row, (list, tuple)):
        return tuple(_norm(value) for value in row[:3])  # type: ignore[return-value]
    return (
        _norm(row.get("source", "")),
        _norm(row.get("relation", "")),
        _norm(row.get("target", "")),
    )


def _counts(expected: Iterable[Any], predicted: Iterable[Any], key) -> Counts:
    expected_keys = {key(row) for row in expected}
    predicted_keys = {key(row) for row in predicted}
    return Counts(
        correct=len(expected_keys & predicted_keys),
        predicted=len(predicted_keys),
        expected=len(expected_keys),
    )


def _wilson_lower(correct: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    ratio = correct / total
    denominator = 1 + (z * z / total)
    center = ratio + (z * z / (2 * total))
    margin = z * math.sqrt((ratio * (1 - ratio) / total) + (z * z / (4 * total * total)))
    return max(0.0, (center - margin) / denominator)


def _metric(counts: Counts, target: float) -> dict[str, Any]:
    precision = counts.correct / counts.predicted if counts.predicted else 0.0
    recall = counts.correct / counts.expected if counts.expected else 1.0
    lower = _wilson_lower(counts.correct, counts.predicted)
    return {
        **asdict(counts),
        "precision": precision,
        "recall": recall,
        "wilson_95_lower": lower,
        "target": target,
        "pass": precision >= target and lower >= target,
    }


def load_fixture(path: Path, *, min_cases: int) -> list[dict[str, Any]]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    ids = [str(case.get("id", "")).strip() for case in cases]
    if len(cases) < min_cases:
        raise ValueError(f"fixture has {len(cases)} cases; at least {min_cases} are required")
    if any(not case_id for case_id in ids) or len(set(ids)) != len(ids):
        raise ValueError("fixture case IDs must be non-empty and unique")
    if any(case.get("synthetic") is not True for case in cases):
        raise ValueError("private quality fixtures must be explicitly synthetic")
    quality_cases = [
        case
        for case in cases
        if case.get("input", {}).get("kind") == "text"
        and case.get("quality_gate") is True
        and {"claims", "entities", "relations"}
        <= set(case.get("expected", {}))
    ]
    if len(quality_cases) < min_cases:
        raise ValueError(
            f"fixture has {len(quality_cases)} inline-text quality cases; "
            f"at least {min_cases} are required"
        )
    return quality_cases


def _claims_for_evidence(service: MemoryService, evidence_id: int) -> list[Any]:
    with contextlib.closing(service.store.connect()) as conn:
        rows = conn.execute(
            """SELECT c.id FROM claims c
               JOIN claim_evidence_links l ON l.claim_id=c.id
               WHERE l.evidence_item_id=? AND c.status='candidate'
               ORDER BY c.id""",
            (evidence_id,),
        ).fetchall()
    return [service.store.get_claim(int(row[0])) for row in rows]


def _graph_rows(service: MemoryService, claim_ids: list[int]) -> tuple[list[str], list[dict[str, str]]]:
    if not claim_ids:
        return [], []
    marks = ",".join("?" for _ in claim_ids)
    with contextlib.closing(service.store.connect()) as conn:
        entities = conn.execute(
            f"""SELECT DISTINCT e.canonical_name FROM entities e
                 JOIN claim_entity_links l ON l.entity_id=e.id
                 WHERE l.claim_id IN ({marks})""",
            claim_ids,
        ).fetchall()
        relations = conn.execute(
            f"""SELECT s.canonical_name, x.relation, t.canonical_name
                 FROM entity_edge_supports x
                 JOIN entities s ON s.id=x.source_entity_id
                 JOIN entities t ON t.id=x.target_entity_id
                 WHERE x.supporting_claim_id IN ({marks})""",
            claim_ids,
        ).fetchall()
    return [str(row[0]) for row in entities], [
        {"source": str(row[0]), "relation": str(row[1]), "target": str(row[2])}
        for row in relations
    ]


def _evaluate_case(service: MemoryService, db: Path, workspace: Path, case: dict[str, Any]) -> dict[str, Any]:
    case_id = str(case["id"])
    scope = f"project:capture-quality-{case_id}"
    receipt = remember(
        text=str(case["input"]["text"]),
        scope=scope,
        db=db,
        workspace=workspace,
    )
    claim_run = run_capture_worker(service, owner=f"quality-{case_id}", limit=1)
    claims = _claims_for_evidence(service, int(receipt.evidence["id"]))
    claim_ids = [int(claim.id) for claim in claims if claim is not None]
    for claim in claims:
        if claim is not None:
            service.store.apply_status_transition(
                claim,
                to_status="confirmed",
                reason="private synthetic quality evaluation",
                event_type="validator",
            )
    if claim_ids:
        improve(scope=scope, max_items=200, db=db, workspace=workspace)
        graph_run = run_capture_worker(
            service, owner=f"quality-graph-{case_id}", limit=len(claim_ids)
        )
    else:
        graph_run = None
    entities, relations = _graph_rows(service, claim_ids)
    expected = case["expected"]
    return {
        "id": case_id,
        "candidate": asdict(_counts(expected["claims"], claims, _claim_key)),
        "candidate_type_exact": asdict(
            _counts(expected["claims"], claims, _typed_claim_key)
        ),
        "entity": asdict(_counts(expected["entities"], entities, _norm)),
        "relationship": asdict(
            _counts(expected["relations"], relations, _relation_key)
        ),
        "claim_job": asdict(claim_run),
        "graph_job": asdict(graph_run) if graph_run else None,
    }


def evaluate(cases: list[dict[str, Any]], temp_root: Path) -> dict[str, Any]:
    temp_dir = Path(tempfile.mkdtemp(prefix="capture-quality-", dir=temp_root))
    workspace = temp_dir / "workspace"
    workspace.mkdir()
    db = temp_dir / "quality.db"
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()
    case_results: list[dict[str, Any]] = []
    try:
        for case in cases:
            case_results.append(_evaluate_case(service, db, workspace, case))
        metrics = {}
        for name, target in TARGETS.items():
            totals = Counts(
                correct=sum(row[name]["correct"] for row in case_results),
                predicted=sum(row[name]["predicted"] for row in case_results),
                expected=sum(row[name]["expected"] for row in case_results),
            )
            metrics[name] = _metric(totals, target)
        versions = sorted(
            {
                str(client._cached_version)
                for client in llm_provider._OPENCODE_CLIENTS.values()
                if getattr(client, "_cached_version", None)
            }
        )
        typed = Counts(
            correct=sum(row["candidate_type_exact"]["correct"] for row in case_results),
            predicted=sum(row["candidate_type_exact"]["predicted"] for row in case_results),
            expected=sum(row["candidate_type_exact"]["expected"] for row in case_results),
        )
        return {
            "schema_version": "memorymaster.capture-graph-quality.v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "case_count": len(cases),
            "provider": os.environ.get("MEMORYMASTER_CAPTURE_LLM_PROVIDER", ""),
            "model": os.environ.get("MEMORYMASTER_CAPTURE_LLM_MODEL", ""),
            "effort": os.environ.get("MEMORYMASTER_CAPTURE_LLM_REASONING_EFFORT", ""),
            "opencode_versions": versions,
            "metrics": metrics,
            "diagnostics": {
                "candidate_type_exact": {
                    **asdict(typed),
                    "accuracy": typed.correct / typed.expected if typed.expected else 1.0,
                }
            },
            "gate_pass": all(row["pass"] for row in metrics.values()),
            "cases": case_results,
        }
    finally:
        del service
        gc.collect()
        shutil.rmtree(temp_dir, ignore_errors=True)


def _private_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError:
        return resolved
    raise ValueError("quality results must be written outside the repository")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--min-cases", type=int, default=40)
    parser.add_argument("--max-cases", type=int, default=0)
    parser.add_argument("--provider", default="opencode")
    parser.add_argument("--model", default="openai/gpt-5.6-terra")
    parser.add_argument("--effort", default="medium")
    args = parser.parse_args()
    cases = load_fixture(args.fixture.expanduser(), min_cases=args.min_cases)
    if args.max_cases:
        cases = cases[: args.max_cases]
    os.environ["MEMORYMASTER_CAPTURE_LLM_PROVIDER"] = args.provider
    os.environ["MEMORYMASTER_CAPTURE_LLM_MODEL"] = args.model
    os.environ["MEMORYMASTER_CAPTURE_LLM_REASONING_EFFORT"] = args.effort
    output = args.output or args.fixture.parent / "quality-result.json"
    output = _private_output(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    result = evaluate(cases, output.parent)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "gate_pass": result["gate_pass"], "metrics": result["metrics"]}))
    return 0 if result["gate_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
