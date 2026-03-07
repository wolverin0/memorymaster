from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memorymaster.service import MemoryService


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def resolve_benchmark_paths(spec: str, root: Path) -> list[Path]:
    paths: list[Path] = []
    for raw in spec.split(","):
        part = raw.strip()
        if not part:
            continue
        p = root / part
        if p.exists():
            paths.append(p)
            continue
        matches = sorted(root.glob(part))
        paths.extend(matches)

    if not paths:
        raise FileNotFoundError(f"No benchmark files matched: {spec}")
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def load_benchmark_sets(spec: str, root: Path) -> list[dict[str, Any]]:
    paths = resolve_benchmark_paths(spec, root)
    all_cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for p in paths:
        for case in load_jsonl(p):
            cid = str(case.get("id"))
            if not cid:
                raise ValueError(f"Case without id in {p}")
            if cid in seen_ids:
                raise ValueError(f"Duplicate case id found: {cid}")
            seen_ids.add(cid)
            all_cases.append(case)
    return all_cases


def get_nested(data: dict[str, Any], path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def match_claim(claim: Any, spec: dict[str, Any]) -> bool:
    if "subject" in spec and claim.subject != spec["subject"]:
        return False
    if "predicate" in spec and claim.predicate != spec["predicate"]:
        return False
    if "object_value" in spec and claim.object_value != spec["object_value"]:
        return False
    if "status" in spec and claim.status != spec["status"]:
        return False
    if "status_in" in spec and claim.status not in spec["status_in"]:
        return False
    if "text_contains" in spec and spec["text_contains"] not in claim.text:
        return False
    return True


@dataclass(slots=True)
class EvalCounters:
    state_total: int = 0
    state_pass: int = 0
    retrieval_total: int = 0
    retrieval_pass: int = 0
    cycle_total: int = 0
    cycle_pass: int = 0
    security_total: int = 0
    security_pass: int = 0

    def ratios(self) -> dict[str, float]:
        def frac(p: int, t: int) -> float:
            return float(p) / float(t) if t > 0 else 1.0

        state = frac(self.state_pass, self.state_total)
        retrieval = frac(self.retrieval_pass, self.retrieval_total)
        cycle = frac(self.cycle_pass, self.cycle_total)
        security = frac(self.security_pass, self.security_total)
        overall = (state + retrieval + cycle + security) / 4.0
        return {
            "state_accuracy": state,
            "retrieval_accuracy": retrieval,
            "cycle_accuracy": cycle,
            "security_accuracy": security,
            "overall": overall,
        }


def run_step(step: dict[str, Any], service: MemoryService, db_path: Path, cycle_results: list[dict[str, Any]]) -> None:
    action = step.get("action")
    if action == "ingest":
        sources = step.get("sources", [])
        citations = []
        from memorymaster.models import CitationInput

        for raw in sources:
            parts = [part.strip() for part in str(raw).split("|", 2)]
            source = parts[0] if parts else ""
            if not source:
                continue
            locator = parts[1] if len(parts) > 1 and parts[1] else None
            excerpt = parts[2] if len(parts) > 2 and parts[2] else None
            citations.append(CitationInput(source=source, locator=locator, excerpt=excerpt))
        service.ingest(
            text=step["text"],
            citations=citations,
            claim_type=step.get("claim_type"),
            subject=step.get("subject"),
            predicate=step.get("predicate"),
            object_value=step.get("object_value"),
            scope=step.get("scope", "project"),
            volatility=step.get("volatility", "medium"),
            confidence=float(step.get("confidence", 0.5)),
        )
        return

    if action == "run_cycle":
        result = service.run_cycle(
            run_compactor=bool(step.get("with_compact", False)),
            min_citations=int(step.get("min_citations", 1)),
            min_score=float(step.get("min_score", 0.58)),
            policy_mode=str(step.get("policy_mode", "legacy")),
            policy_limit=int(step.get("policy_limit", 200)),
        )
        cycle_results.append(result)
        return

    if action == "sql":
        sql = str(step["sql"])
        # SQL step is supported only for SQLite DB files.
        con = sqlite3.connect(str(db_path))
        try:
            con.execute(sql)
            con.commit()
        finally:
            con.close()
        return

    raise ValueError(f"Unknown step action: {action}")


def evaluate_case(
    case: dict[str, Any],
    *,
    root: Path,
    db_dir: Path,
    counters: EvalCounters,
) -> dict[str, Any]:
    case_id = str(case["id"])
    db_path = db_dir / f"{case_id}.db"
    if db_path.exists():
        db_path.unlink()

    service = MemoryService(db_path, workspace_root=root)
    service.init_db()
    cycle_results: list[dict[str, Any]] = []

    for step in case.get("steps", []):
        run_step(step, service, db_path, cycle_results)

    claims = service.list_claims(include_archived=True, limit=2000)
    failures: list[str] = []
    checks: list[dict[str, Any]] = []

    expectations = case.get("expectations", {})
    for spec in expectations.get("claims", []):
        counters.state_total += 1
        matched = any(match_claim(claim, spec) for claim in claims)
        if matched:
            counters.state_pass += 1
        else:
            failures.append(f"claim_expectation_failed:{spec}")
        checks.append({"kind": "claim", "spec": spec, "passed": matched})

    for ccheck in expectations.get("cycle_checks", []):
        counters.cycle_total += 1
        path = str(ccheck["path"])
        minimum = float(ccheck.get("min", 0))
        latest = cycle_results[-1] if cycle_results else {}
        value = get_nested(latest, path)
        passed = value is not None and float(value) >= minimum
        if passed:
            counters.cycle_pass += 1
        else:
            failures.append(f"cycle_check_failed:{path}:value={value}:min={minimum}")
        checks.append({"kind": "cycle", "path": path, "min": minimum, "value": value, "passed": passed})

    for qspec in expectations.get("queries", []):
        query = str(qspec["query"])
        retrieval_mode = str(qspec.get("retrieval_mode", "hybrid"))
        allow_sensitive = bool(qspec.get("allow_sensitive", False))
        k = int(qspec.get("k", 10))
        rows = service.query(query, limit=k, retrieval_mode=retrieval_mode, allow_sensitive=allow_sensitive)
        claim_texts = [row.text for row in rows]
        claim_objects = [row.object_value or "" for row in rows]

        includes_obj = qspec.get("must_include_objects", [])
        for obj in includes_obj:
            counters.retrieval_total += 1
            passed = any(obj == value for value in claim_objects)
            if passed:
                counters.retrieval_pass += 1
            else:
                failures.append(f"query_missing_object:{query}:{obj}")
            checks.append({"kind": "query_include_object", "query": query, "value": obj, "passed": passed})

        includes_text = qspec.get("must_include_text", [])
        for needle in includes_text:
            counters.retrieval_total += 1
            passed = any(needle in text for text in claim_texts)
            if passed:
                counters.retrieval_pass += 1
            else:
                failures.append(f"query_missing_text:{query}:{needle}")
            checks.append({"kind": "query_include_text", "query": query, "value": needle, "passed": passed})

        excludes_text = qspec.get("must_exclude_text", [])
        for needle in excludes_text:
            counters.retrieval_total += 1
            passed = all(needle not in text for text in claim_texts)
            if passed:
                counters.retrieval_pass += 1
            else:
                failures.append(f"query_unexpected_text:{query}:{needle}")
            checks.append({"kind": "query_exclude_text", "query": query, "value": needle, "passed": passed})

        if "max_rows" in qspec:
            counters.security_total += 1
            passed = len(rows) <= int(qspec["max_rows"])
            if passed:
                counters.security_pass += 1
            else:
                failures.append(f"query_max_rows_failed:{query}:rows={len(rows)}")
            checks.append({"kind": "query_max_rows", "query": query, "max_rows": int(qspec["max_rows"]), "rows": len(rows), "passed": passed})

    return {
        "id": case_id,
        "description": case.get("description", ""),
        "tags": case.get("tags", []),
        "passed": len(failures) == 0,
        "failures": failures,
        "checks": checks,
        "cycle_results": cycle_results,
        "claims_snapshot": [asdict(claim) for claim in claims[:50]],
    }


def write_markdown_report(path: Path, run: dict[str, Any]) -> None:
    lines: list[str] = []
    lines.append(f"# MemoryMaster Evaluation Report")
    lines.append("")
    lines.append(f"- timestamp: `{run['timestamp']}`")
    lines.append(f"- passed: `{run['passed']}`")
    lines.append(f"- cases: `{run['summary']['cases_passed']}/{run['summary']['cases_total']}`")
    lines.append("- benchmark_files:")
    for benchmark in run.get("benchmark_files", []):
        lines.append(f"  - `{benchmark}`")
    lines.append("")
    lines.append("## Metrics")
    metrics = run["summary"]["metrics"]
    for key, value in metrics.items():
        lines.append(f"- {key}: `{value:.4f}`")
    lines.append("")
    tag_summary = run["summary"].get("tag_summary", {})
    if tag_summary:
        lines.append("## Tag Summary")
        for tag, data in sorted(tag_summary.items()):
            lines.append(f"- `{tag}`: `{data['passed']}/{data['total']}`")
        lines.append("")
    lines.append("## Cases")
    for case in run["cases"]:
        lines.append(f"- `{case['id']}`: `{'PASS' if case['passed'] else 'FAIL'}`")
        if case["failures"]:
            for failure in case["failures"]:
                lines.append(f"  - {failure}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run synthetic robustness evaluation for MemoryMaster.")
    parser.add_argument(
        "--benchmarks",
        default="benchmarks/cases.jsonl,benchmarks/cases_general.jsonl,benchmarks/cases_adversarial.jsonl",
        help="Comma-separated benchmark file paths or globs",
    )
    parser.add_argument("--thresholds", default="benchmarks/thresholds.json")
    parser.add_argument("--db-dir", default="artifacts/eval/db")
    parser.add_argument("--out-json", default="artifacts/eval/eval_results.json")
    parser.add_argument("--out-md", default="artifacts/eval/eval_report.md")
    parser.add_argument("--out-csv", default="artifacts/eval/eval_checks.csv")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when thresholds are not met.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path.cwd()
    threshold_path = root / args.thresholds
    db_dir = root / args.db_dir
    out_json = root / args.out_json
    out_md = root / args.out_md
    out_csv = root / args.out_csv

    db_dir.mkdir(parents=True, exist_ok=True)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    benchmark_paths = resolve_benchmark_paths(args.benchmarks, root)
    cases = load_benchmark_sets(args.benchmarks, root)
    thresholds = json.loads(threshold_path.read_text(encoding="utf-8"))
    counters = EvalCounters()
    case_results: list[dict[str, Any]] = []

    for case in cases:
        case_results.append(evaluate_case(case, root=root, db_dir=db_dir, counters=counters))

    metrics = counters.ratios()
    threshold_failures: list[str] = []
    for key, min_value in thresholds.items():
        metric_key = key.replace("_min", "")
        actual = metrics.get(metric_key)
        if actual is None:
            continue
        if actual < float(min_value):
            threshold_failures.append(f"{metric_key}<{min_value} (actual={actual:.4f})")

    cases_passed = sum(1 for case in case_results if case["passed"])
    run = {
        "timestamp": utc_now(),
        "benchmark_files": [str(path) for path in benchmark_paths],
        "passed": len(threshold_failures) == 0 and cases_passed == len(case_results),
        "summary": {
            "cases_total": len(case_results),
            "cases_passed": cases_passed,
            "metrics": metrics,
            "threshold_failures": threshold_failures,
        },
        "cases": case_results,
    }

    tag_summary: dict[str, dict[str, int]] = {}
    for case in case_results:
        for tag in case.get("tags", []):
            if tag not in tag_summary:
                tag_summary[tag] = {"total": 0, "passed": 0}
            tag_summary[tag]["total"] += 1
            if case["passed"]:
                tag_summary[tag]["passed"] += 1
    run["summary"]["tag_summary"] = tag_summary

    out_json.write_text(json.dumps(run, indent=2), encoding="utf-8")
    write_markdown_report(out_md, run)
    with out_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["case_id", "kind", "passed", "detail"],
        )
        writer.writeheader()
        for case in case_results:
            for check in case["checks"]:
                writer.writerow(
                    {
                        "case_id": case["id"],
                        "kind": str(check.get("kind", "")),
                        "passed": bool(check.get("passed", False)),
                        "detail": json.dumps(check, ensure_ascii=True),
                    }
                )

    print(json.dumps(run["summary"], indent=2))
    if args.strict and (not run["passed"]):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
