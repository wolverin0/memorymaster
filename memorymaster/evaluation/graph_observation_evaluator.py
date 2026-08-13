"""Offline structural evaluator for the versioned PPR-7 synthetic corpus."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memorymaster.knowledge.graph_observations import (
    ObservationSupport,
    canonical_signature,
    discover_components,
)


DEFAULT_CORPUS = Path(__file__).resolve().parents[2] / "benchmarks" / "fixtures" / "graph_observations_v1.json"
EXCLUDED_STATES = frozenset(
    {
        "candidate",
        "conflicted",
        "observation",
        "observer_agent",
        "retired",
        "sensitive_claim",
        "sensitive_evidence",
        "sensitive_source",
        "skill",
        "stale",
        "summary",
        "wrong_scope",
        "wrong_tenant",
    }
)


def _row(claim: int, evidence: int, source: int, edge: tuple[int, str, int]) -> ObservationSupport:
    signature = canonical_signature(
        edge[0],
        edge[1],
        edge[2],
        "personal-v1",
        symmetric_relations=frozenset({"related_to"}),
    )
    return ObservationSupport(
        claim,
        evidence,
        source,
        signature[0],
        signature[1],
        signature[2],
        signature[3],
        "project:evaluator",
        "tenant-evaluator",
        0.8,
        f"2026-08-{min(evidence, 28):02d}T00:00:00+00:00",
    )


def _eligible(offset: int = 0) -> list[ObservationSupport]:
    return [
        _row(1 + offset, 1 + offset, 101 + offset, (10 + offset, "depends_on", 20 + offset)),
        _row(2 + offset, 2 + offset, 102 + offset, (10 + offset, "depends_on", 20 + offset)),
        _row(2 + offset, 2 + offset, 102 + offset, (20 + offset, "depends_on", 30 + offset)),
        _row(3 + offset, 1 + offset, 101 + offset, (20 + offset, "depends_on", 30 + offset)),
    ]


def _template(name: str) -> list[ObservationSupport]:
    if name in {"eligible", "cross_scope", "cross_tenant", "excluded"}:
        return _eligible()
    if name == "merge":
        return _eligible() + [_row(4, 3, 103, (20, "depends_on", 30))]
    if name == "split":
        return _eligible() + _eligible(3)
    if name == "unrelated":
        return [_row(i, i, 100 + i, (i, "uses", 50 + i)) for i in range(1, 5)]
    if name == "insufficient_claims":
        return _eligible()[:2]
    if name == "insufficient_evidence":
        return [_row(i, 1, 101, (10 + i, "uses", 20 + i)) for i in range(1, 4)]
    if name == "one_signature":
        return [_row(i, 1 + i % 2, 101 + i % 2, (10, "uses", 20)) for i in range(1, 4)]
    if name == "symmetric":
        return [
            _row(1, 1, 101, (10, "related_to", 20)),
            _row(2, 2, 102, (20, "related_to", 10)),
            _row(2, 2, 102, (20, "uses", 30)),
            _row(3, 1, 101, (20, "uses", 30)),
        ]
    if name == "hub":
        return [_row(i, i, 100 + i, (10, "related_to", 20)) for i in range(1, 22)]
    if name == "oversized":
        rows = [_row(i, i, 100 + i, (i, "uses", i + 1)) for i in range(1, 22)]
        rows.extend(_row(i, i, 100 + i, (i - 1, "uses", i)) for i in range(2, 22))
        return rows
    raise ValueError(f"unknown corpus template: {name}")


def _supports(case: dict[str, Any]) -> list[ObservationSupport]:
    state = str(case.get("state") or "")
    if state in EXCLUDED_STATES:
        return []
    return _template(str(case["template"]))


def evaluate_corpus(path: Path = DEFAULT_CORPUS) -> dict[str, Any]:
    """Evaluate exact predicted claim groups against the versioned oracle."""
    corpus = json.loads(path.read_text(encoding="utf-8"))
    tp = fp = fn = 0
    failures: list[str] = []
    for case in corpus["cases"]:
        result = discover_components(_supports(case), scope="project:evaluator", tenant_id="tenant-evaluator")
        predicted = {tuple(component.claim_ids) for component in result.components}
        expected = {tuple(group) for group in case["expected"]}
        tp += len(predicted & expected)
        fp += len(predicted - expected)
        fn += len(expected - predicted)
        if predicted != expected:
            failures.append(str(case["id"]))
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {
        "corpus_version": corpus["corpus_version"],
        "cases": len(corpus["cases"]),
        "precision": precision,
        "recall": recall,
        "failures": failures,
    }


def main() -> int:
    report = evaluate_corpus()
    print(json.dumps(report, sort_keys=True))
    return int(report["precision"] < 0.95 or report["recall"] < 0.95)


if __name__ == "__main__":
    raise SystemExit(main())
