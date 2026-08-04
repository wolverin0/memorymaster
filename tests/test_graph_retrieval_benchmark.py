from __future__ import annotations

from benchmarks.graph_retrieval_benchmark import run_benchmark


def test_graph_benchmark_is_deterministic_and_governed() -> None:
    first = run_benchmark(case_limit=2)
    second = run_benchmark(case_limit=2)

    assert first["questions"] == second["questions"] == 2
    assert first["hits"] == second["hits"]
    assert first["forbidden_hits"] == second["forbidden_hits"] == 0
    assert first["provider_calls"] == second["provider_calls"] == 0
    assert [row["hit"] for row in first["details"]] == [
        row["hit"] for row in second["details"]
    ]
