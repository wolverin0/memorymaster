"""Integration tests for the consolidated eval harness (roadmap 11.7).

The harness now invokes production ``context_hook.recall()`` end-to-end and
reads claim IDs out of the opt-in ``return_ids=True`` return tuple. These
tests exercise the full pipeline against a **real** temp DB seeded with 20
claims — NO mocks on ``recall`` — and assert deterministic top-5 ordering
for a 3-prompt fixture.

Covers:

1. ``recall(return_ids=True)`` returns ``(str, list[int])`` and the ID
   order matches the bullet order in the rendered markdown.
2. The harness ``run_eval`` executes without error on a clean DB and
   reports per-prompt p@5 / MAP@5 / latency records.
3. Top-5 ordering is deterministic: two consecutive runs over the same
   fixture DB return identical claim-ID sequences per prompt.
4. Missing labels side-file does NOT crash — the harness falls back to
   the heuristic overlap scorer seamlessly.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from memorymaster.recall.context_hook import recall
from memorymaster.models import CitationInput
from memorymaster.service import MemoryService

REPO = Path(__file__).resolve().parent.parent
HARNESS_PATH = REPO / "scripts" / "eval_recall_precision_at_5.py"


def _load_harness_module():
    """Import the eval script as a module — it's not package-installed.

    The harness defines frozen dataclasses at module scope; dataclass's
    ``_is_type`` helper inspects ``sys.modules.get(cls.__module__)`` and
    crashes if the module isn't registered, so we insert the spec into
    ``sys.modules`` BEFORE exec_module.
    """
    mod_name = "eval_harness"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
    return mod


# 20 seeded claims grouped into three topic clusters so the three fixture
# prompts have well-separated expected top hits. Each entry is the text +
# subject seed; scope is ``project:memorymaster`` for everything to keep
# scope-boost off the critical path.
_SEED_CLAIMS = [
    # Cluster A — steward classifier (prompt 1 target)
    ("Steward classifier v3 outperforms v2 on both data splits and will be promoted.", "steward-classifier-v3"),
    ("Steward classifier calibration run shipped on 2026-04-22.", "steward-classifier-calibration"),
    ("Steward classifier v2 trained on 10775 rows with balanced precision.", "steward-classifier-v2"),
    ("Classifier v3 backtest produced an F1 +0.02 lift over v1.", "classifier-backtest"),
    ("Archived claims stop accruing at 2026-04-18 in the chronological split.", "archived-window"),
    ("Steward cycle runs every six hours and promotes candidate claims.", "steward-cycle"),
    ("Steward metadata includes confidence score and freshness score.", "steward-signals"),
    # Cluster B — recall ranker (prompt 2 target)
    ("BM25 lexical rescorer k1=1.2 b=0.25 shipped by default in recall hook.", "bm25-rescorer"),
    ("Recall hook pre-tokenises queries before hitting FTS5.", "recall-tokenizer"),
    ("Recall weights are configured via MEMORYMASTER_RECALL_W_* env vars.", "recall-weights"),
    ("RRF fusion mode merges per-stream rankings instead of linear weights.", "rrf-fusion"),
    ("Entity-link fanout rescues zero-hit prompts by mining prompt entities.", "entity-fanout"),
    ("Scope-aware retrieval boost multiplies relevance for matching-scope claims.", "scope-boost"),
    # Cluster C — MCP server (prompt 3 target)
    ("MCP server exposes twenty-one tools including ingest_claim and query_memory.", "mcp-tools"),
    ("MCP auto-citation fallback wraps tool calls missing explicit citations.", "mcp-citation"),
    ("MCP ingest route passes through the sensitivity filter by default.", "mcp-sensitivity"),
    ("FastMCP stdio transport is the shipped protocol for MemoryMaster.", "mcp-transport"),
    # Distractors — unrelated but non-trivial
    ("Obsidian vault stores compiled wiki articles by project scope.", "obsidian-vault"),
    ("Qdrant vector search supplies a semantic fallback for sparse FTS hits.", "qdrant-fallback"),
    ("SQLite WAL mode is mandatory to prevent corruption under concurrency.", "sqlite-wal"),
]

_FIXTURE_PROMPTS = [
    "steward classifier v3 promoted",
    "bm25 lexical rescorer recall hook",
    "mcp server tools for ingestion",
]


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Create a fresh DB seeded with _SEED_CLAIMS. Module-scoped so the 20
    ingest calls run once across the whole test file — they dominate the
    test wall time and are fully deterministic given identical inputs."""
    tmp = tmp_path_factory.mktemp("eval_harness_db")
    db = tmp / "test.db"
    svc = MemoryService(db_target=str(db), workspace_root=tmp)
    svc.init_db()
    for text, subject in _SEED_CLAIMS:
        svc.ingest(
            text=text,
            citations=[CitationInput(source="test-harness")],
            subject=subject,
            claim_type="fact",
            scope="project:memorymaster",
            confidence=0.7,
            source_agent="test",
        )
    return db


# --------------------------------------------------------------------------- #
# Layer 1 — return_ids contract
# --------------------------------------------------------------------------- #


def test_recall_return_ids_is_opt_in(
    seeded_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default return is bare str; return_ids=True returns (str, list[int])."""
    # Avoid noisy env overrides from the host environment.
    for name in ("MEMORYMASTER_RECALL_FUSION", "MEMORYMASTER_RECALL_VERBATIM"):
        monkeypatch.delenv(name, raising=False)

    default_out = recall("steward classifier", db_path=str(seeded_db),
                         skip_qdrant=True)
    assert isinstance(default_out, str)
    assert "Memory Context" in default_out

    tuple_out = recall("steward classifier", db_path=str(seeded_db),
                       skip_qdrant=True, return_ids=True)
    assert isinstance(tuple_out, tuple)
    assert len(tuple_out) == 2
    markdown, ids = tuple_out
    assert isinstance(markdown, str)
    assert isinstance(ids, list)
    assert ids, "expected at least one claim id back"
    assert all(isinstance(cid, int) for cid in ids)


def test_recall_return_ids_matches_bullet_order(
    seeded_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ID list is in the same order as the markdown bullet lines."""
    for name in ("MEMORYMASTER_RECALL_FUSION", "MEMORYMASTER_RECALL_VERBATIM"):
        monkeypatch.delenv(name, raising=False)

    markdown, ids = recall("recall ranker weights",
                           db_path=str(seeded_db),
                           skip_qdrant=True, return_ids=True)
    bullets = [ln for ln in markdown.splitlines() if ln.startswith("- ")]
    assert len(bullets) == len(ids), (
        f"bullet/id length mismatch: {len(bullets)} bullets vs "
        f"{len(ids)} ids in {markdown!r}"
    )


# --------------------------------------------------------------------------- #
# Layer 2 — harness end-to-end
# --------------------------------------------------------------------------- #


def test_harness_runs_against_seeded_db_without_mocks(
    seeded_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_eval executes fully against a real DB — no mocks on recall()."""
    # Stable env — the tests must not depend on host overrides.
    for name in ("MEMORYMASTER_RECALL_FUSION", "MEMORYMASTER_RECALL_VERBATIM",
                 "MEMORYMASTER_RECALL_VECTOR_FALLBACK"):
        monkeypatch.delenv(name, raising=False)

    mod = _load_harness_module()

    prompts_path = tmp_path / "prompts.jsonl"
    with prompts_path.open("w", encoding="utf-8") as fh:
        for text in _FIXTURE_PROMPTS:
            fh.write(json.dumps({"text": text}) + "\n")

    prompts = mod._load_prompts(prompts_path)
    assert len(prompts) == 3
    assert all(rec.sha == mod._sha16(rec.text) for rec in prompts)

    # No labels file → fall back to heuristic.
    results = mod.run_eval(
        prompts=prompts,
        db_path=str(seeded_db),
        ground_truth={},
        min_overlap=2,
    )
    assert len(results) == 3
    for r in results:
        assert r.returned_ids, "each fixture prompt should surface at least one claim"
        assert 0.0 <= r.p5 <= 1.0
        assert 0.0 <= r.ap5 <= 1.0
        assert r.latency_ms > 0.0
        assert r.label_source == "heuristic"
        # Labels list must align with returned_ids length
        assert len(r.labels) == len(r.returned_ids)


def test_harness_top5_is_deterministic(
    seeded_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running the harness twice over the same fixture yields identical
    top-5 claim IDs per prompt. This is the regression bar — any future
    non-determinism in the ranker will trip it."""
    for name in ("MEMORYMASTER_RECALL_FUSION", "MEMORYMASTER_RECALL_VERBATIM",
                 "MEMORYMASTER_RECALL_VECTOR_FALLBACK"):
        monkeypatch.delenv(name, raising=False)

    def _run_once() -> list[tuple[int, ...]]:
        runs: list[tuple[int, ...]] = []
        for text in _FIXTURE_PROMPTS:
            _md, ids = recall(text, db_path=str(seeded_db),
                              skip_qdrant=True, return_ids=True)
            runs.append(tuple(ids[:5]))
        return runs

    first = _run_once()
    second = _run_once()
    assert first == second, (
        f"top-5 ordering drifted between runs:\n"
        f"  run-1: {first}\n  run-2: {second}"
    )


def test_harness_uses_ground_truth_when_present(
    seeded_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a labels side-file exists, the harness honours it over the
    heuristic. We build a file that marks ALL claim IDs as relevant for
    prompt 1, and ZERO as relevant for prompts 2+3 — p@5 for prompt 1
    should therefore be 1.0 and for 2/3 should be 0.0 regardless of text
    overlap."""
    for name in ("MEMORYMASTER_RECALL_FUSION", "MEMORYMASTER_RECALL_VERBATIM"):
        monkeypatch.delenv(name, raising=False)

    mod = _load_harness_module()

    # Run once to discover what ids recall() returns for each prompt; we
    # need those ids in the labels file for prompt 0.
    prompt1 = _FIXTURE_PROMPTS[0]
    _md, ids_p1 = recall(prompt1, db_path=str(seeded_db),
                         skip_qdrant=True, return_ids=True)
    assert ids_p1, "prompt 1 must surface claims or the test is vacuous"

    labels_payload = {
        "min_overlap": 3,
        "top_k": 20,
        "labels": {
            mod._sha16(prompt1): list(ids_p1),
            # Prompts 2 and 3 — empty relevant lists means nothing returned
            # can be a hit.
            mod._sha16(_FIXTURE_PROMPTS[1]): [],
            mod._sha16(_FIXTURE_PROMPTS[2]): [],
        },
    }
    labels_path = tmp_path / "fixture-labels.json"
    with labels_path.open("w", encoding="utf-8") as fh:
        json.dump(labels_payload, fh)

    ground_truth, min_overlap = mod._load_labels(labels_path)
    assert len(ground_truth) == 3
    assert min_overlap == 3

    prompts_path = tmp_path / "fixture.jsonl"
    with prompts_path.open("w", encoding="utf-8") as fh:
        for text in _FIXTURE_PROMPTS:
            fh.write(json.dumps({"text": text}) + "\n")

    prompts = mod._load_prompts(prompts_path)
    results = mod.run_eval(
        prompts=prompts,
        db_path=str(seeded_db),
        ground_truth=ground_truth,
        min_overlap=min_overlap,
    )
    assert results[0].label_source == "ground_truth"
    assert results[0].p5 > 0.0, "prompt 1 must score above zero on GT labels"
    assert results[1].label_source == "ground_truth"
    assert results[1].p5 == 0.0
    assert results[2].label_source == "ground_truth"
    assert results[2].p5 == 0.0
