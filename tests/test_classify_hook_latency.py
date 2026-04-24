"""Latency budget guard for the classify hook.

The classify hook (`memorymaster/config_templates/hooks/memorymaster-classify.py`)
runs on EVERY UserPromptSubmit event — if it blows past ~15ms it becomes
a felt-latency tax on every prompt. This test imports the module's
`classify()` function directly (bypasses stdio parsing) and asserts
median runtime stays under budget on a fixture of realistic prompts.

Per claim 11848: use perf_counter, not monotonic (Windows 15.6ms floor).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from time import perf_counter

import pytest

_LATENCY_BUDGET_MS = 15.0
_WARMUP_RUNS = 2
_MEASURED_RUNS = 20

_FIXTURE_PROMPTS = [
    "Let's decide the schema: I'm thinking one table per entity kind.",
    "we got burned last quarter when the mock passed but prod failed",
    "from now on when I push to main, run the tests",
    "stop doing that, it breaks the build",
    "I think the bug is in _relevance — can you reproduce?",
    "set MEMORYMASTER_RECALL_FUSION=rrf for the benchmark",
    "check the Linear project INGEST for pipeline bugs",
    "decidamos ya — hace c, no tenemos nada que perder, go",
    "la arquitectura del steward classifier separa features y pesos",
    "esto es un gotcha con windows y wsl spawn",
    "? "
    * 50,
    "a" * 2000,
]


def _load_classify():
    path = (
        Path(__file__).parent.parent
        / "memorymaster"
        / "config_templates"
        / "hooks"
        / "memorymaster-classify.py"
    )
    spec = importlib.util.spec_from_file_location("mm_classify_hook", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.classify


@pytest.fixture(scope="module")
def classify():
    return _load_classify()


def _measure_one(classify_fn, prompt: str) -> float:
    start = perf_counter()
    classify_fn(prompt)
    return (perf_counter() - start) * 1000.0


def test_classify_hook_under_budget(classify):
    for _ in range(_WARMUP_RUNS):
        for p in _FIXTURE_PROMPTS:
            classify(p)

    samples: list[float] = []
    for _ in range(_MEASURED_RUNS):
        for p in _FIXTURE_PROMPTS:
            samples.append(_measure_one(classify, p))

    samples.sort()
    median = samples[len(samples) // 2]
    p99 = samples[min(len(samples) - 1, int(len(samples) * 0.99))]
    mean = sum(samples) / len(samples)

    assert median < _LATENCY_BUDGET_MS, (
        f"classify hook median latency {median:.2f}ms exceeds "
        f"{_LATENCY_BUDGET_MS}ms budget (p99={p99:.2f}ms, mean={mean:.2f}ms)"
    )


def test_classify_hook_empty_prompt_fast(classify):
    start = perf_counter()
    classify("")
    elapsed_ms = (perf_counter() - start) * 1000.0
    assert elapsed_ms < _LATENCY_BUDGET_MS
