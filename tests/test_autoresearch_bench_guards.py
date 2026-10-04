"""The campaign Guards reject a degraded output (autoresearch first run, 2026-10-04).

A Verify that only times code would accept a faster pack_context that silently drops
a claim, or a tokenizer that picks different tokens. Each benchmark fingerprints its
exact output; these tests degrade the output for real and require the Guard to fail.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"bench_{name}", ROOT / "benchmarks" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pack_context_guard_rejects_a_dropped_claim_and_accepts_identical_output(monkeypatch) -> None:
    bench = _load("pack_context_bench")
    baseline = bench.measure(repeats=1)
    assert bench.guard(bench.measure(repeats=1), baseline) == []

    real = bench.pack_context
    monkeypatch.setattr(bench, "pack_context", lambda rows, **kw: real(rows[:-1], **kw))  # "faster": one claim less
    violations = bench.guard(bench.measure(repeats=1), baseline)
    assert violations and all(v.endswith("fingerprint changed") for v in violations)


def test_tokenizer_guard_rejects_changed_tokens(tmp_path: Path, capsys) -> None:
    bench = _load("tokenizer_cold_bench")
    assert bench.main(["--sizes", "40", "--repeats", "1"]) == 0
    baseline = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    tampered = tmp_path / "baseline.json"
    baseline["cases"]["40"]["fingerprint"] = "0" * 64  # a candidate that selects other tokens
    tampered.write_text(json.dumps(baseline), encoding="utf-8")
    assert bench.main(["--sizes", "40", "--repeats", "1", "--guard", str(tampered)]) == 1
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["violations"] == ["40: tokens changed"]


def test_recall_guard_rejects_a_changed_ranking(tmp_path: Path, capsys) -> None:
    bench = _load("recall_warm_bench")
    assert bench.main(["--size", "200", "--repeats", "1", "--confirmed-share", "0.5"]) == 0
    baseline = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert baseline["returned_any"], "the corpus must reach prompt recall, or the guard checks an empty path"
    query = next(iter(baseline["fingerprints"]))
    baseline["fingerprints"][query] = "0" * 64  # a candidate that reorders or drops claims for this query
    tampered = tmp_path / "baseline.json"
    tampered.write_text(json.dumps(baseline), encoding="utf-8")
    assert bench.main(["--size", "200", "--repeats", "1", "--confirmed-share", "0.5", "--guard", str(tampered)]) == 1
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["violations"] == [f"{query}: ranked ids changed"]


def test_the_recall_bench_restores_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_JEV_MODE", "live")
    _load("recall_warm_bench").measure(size=30, repeats=1, confirmed_share=0.5)
    assert __import__("os").environ["MEMORYMASTER_JEV_MODE"] == "live"
