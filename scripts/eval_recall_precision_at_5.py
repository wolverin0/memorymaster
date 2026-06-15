"""Evaluate production ``context_hook.recall()`` at precision@5 / MAP@5.

Roadmap 11.7 — harness consolidation. Previously this script duplicated the
ranker logic (its own ``_fetch_candidates`` + ``_score``) so ranker-internal
changes (BM25 rescorer, scope-boost, query-expansion, RRF fusion) were
invisible. This rewrite invokes the real production ``recall()`` end-to-end
and evaluates the claim IDs it actually surfaces in the ``# Memory Context``
block.

Key design notes:

* Uses the ``return_ids=True`` opt-in added to
  :func:`memorymaster.recall.context_hook.recall` so we never have to re-match
  rendered bullet text against the DB.
* Read-only against the live DB — monkey-patches the service + store to
  disable every write path.
* Ground-truth labels: when a side-file of the form
  ``<prompts>-labels.json`` exists (e.g.
  ``artifacts/real-prompts-100-labels.json``), its
  ``labels: {prompt_sha1_16: [claim_ids]}`` mapping is consulted first.
  Prompts without an entry fall back to the token-overlap heuristic the
  old harness used — same ``min_overlap`` semantic.
* Tunable feature flags (RRF fusion, scope-boost, query-expansion,
  verbatim, vector fallback, W_* weights) are configured via env vars
  *before* this script runs; the harness reads the environment and
  records it in the JSON summary for reproducibility.

Usage::

    python scripts/eval_recall_precision_at_5.py \
        --prompts artifacts/real-prompts-100.jsonl \
        --db memorymaster.db \
        --json-out artifacts/<run>.jsonl \
        --label cfg
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Only rebind stdout when run as a CLI script. Importing this module from
# pytest (for the test_eval_harness integration tests) MUST NOT swap the
# pytest stdout capture — doing so triggers "I/O operation on closed file"
# at teardown time.
if __name__ == "__main__" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from memorymaster.recall.context_hook import recall  # noqa: E402
from memorymaster.recall.recall_tokenizer import _candidate_tokens  # noqa: E402


# Env flags whose values we capture in the run summary — makes it trivial to
# tell ex-post what feature set a given JSONL came from. We never mutate them.
_TRACKED_ENV = (
    "MEMORYMASTER_RECALL_VERBATIM",
    "MEMORYMASTER_RECALL_VECTOR_FALLBACK",
    "MEMORYMASTER_RECALL_SCOPE_BOOST",
    "MEMORYMASTER_RECALL_QUERY_EXPANSION",
    "MEMORYMASTER_RECALL_FUSION",
    "MEMORYMASTER_LEXICAL_BM25",
    "MEMORYMASTER_RECALL_W_MATCHES",
    "MEMORYMASTER_RECALL_W_PHRASE",
    "MEMORYMASTER_RECALL_W_ALL",
    "MEMORYMASTER_RECALL_W_LEXICAL",
    "MEMORYMASTER_RECALL_W_CONFIDENCE",
    "MEMORYMASTER_RECALL_W_FRESHNESS",
    "MEMORYMASTER_RECALL_W_VECTOR",
    "MEMORYMASTER_RECALL_W_ENTITY",
    "MEMORYMASTER_RECALL_W_VERBATIM",
    "MEMORYMASTER_BM25_K1",
    "MEMORYMASTER_BM25_B",
    "MEMORYMASTER_BM25_W_SUBJECT",
    "MEMORYMASTER_BM25_W_TEXT",
)


@dataclass(frozen=True)
class PromptRecord:
    """One evaluation prompt + its heuristic-label seed tokens."""

    text: str
    sha: str
    prompt_tokens: frozenset[str]


@dataclass(frozen=True)
class PromptEval:
    """Per-prompt evaluation outcome."""

    idx: int
    prompt: str
    sha: str
    returned_ids: tuple[int, ...]
    labels: tuple[int, ...]
    p5: float
    ap5: float
    latency_ms: float
    label_source: str  # "ground_truth" | "heuristic" | "error"


def _sha16(text: str) -> str:
    """SHA1[:16] — matches ``scripts/expand_recall_eval._sha`` so labels
    produced by that script line up with prompts here without a separate
    mapping table.
    """
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _load_prompts(path: Path) -> list[PromptRecord]:
    out: list[PromptRecord] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            text = (rec.get("text") or "").strip()
            if not text:
                continue
            out.append(PromptRecord(
                text=text,
                sha=_sha16(text),
                prompt_tokens=frozenset(
                    t for t in _candidate_tokens(text) if len(t) >= 3
                ),
            ))
    return out


def _load_labels(path: Path) -> tuple[dict[str, set[int]], int]:
    """Load ground-truth labels side-file.

    Returns ``(sha → relevant_ids, min_overlap_used_to_generate_labels)``.
    When the side-file is missing, returns an empty mapping and the
    default min_overlap=2. Missing file is not an error — prompts simply
    fall back to the heuristic overlap scorer.
    """
    if not path.exists():
        return {}, 2
    with path.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    raw = payload.get("labels") or {}
    out: dict[str, set[int]] = {}
    for sha, ids in raw.items():
        if isinstance(sha, str) and isinstance(ids, list):
            out[sha] = {int(i) for i in ids if isinstance(i, int)}
    min_overlap = int(payload.get("min_overlap", 2))
    return out, min_overlap


def _heuristic_relevance(
    claim_text: str,
    claim_subject: str | None,
    prompt_tokens: frozenset[str],
    min_overlap: int,
) -> int:
    """Token-overlap proxy label — matches the old harness semantic."""
    joined = f"{claim_subject or ''} {claim_text or ''}"
    ct = {t for t in _candidate_tokens(joined) if len(t) >= 3}
    return 1 if len(ct & prompt_tokens) >= min_overlap else 0


def _precision_at_k(labels: tuple[int, ...], k: int = 5) -> float:
    head = labels[:k]
    if not head:
        return 0.0
    return sum(head) / len(head)


def _average_precision_at_k(labels: tuple[int, ...], k: int = 5) -> float:
    head = labels[:k]
    if not head:
        return 0.0
    hits = 0
    total = 0.0
    for i, lab in enumerate(head, 1):
        if lab:
            hits += 1
            total += hits / i
    n_rel = max(1, sum(head))
    return total / n_rel


def _patch_service_readonly() -> None:
    """Disable every write path on ``MemoryService`` + its store.

    We install this globally BEFORE any ``recall()`` call so the hot loop
    never mutates ``claim_accesses`` / ``claim_signals`` — necessary when
    the harness runs against the live 7.8 GB DB.
    """
    from memorymaster.core import service as _svc_mod

    _original_init = _svc_mod.MemoryService.__init__

    def _ro_init(self, *a, **kw):
        _original_init(self, *a, **kw)
        self._record_accesses = lambda *a, **k: None
        if hasattr(self, "store") and hasattr(self.store, "record_accesses_batch"):
            self.store.record_accesses_batch = lambda *a, **k: None

    _svc_mod.MemoryService.__init__ = _ro_init


def _lookup_claim_texts(db_path: str, ids: list[int]) -> dict[int, tuple[str, str | None]]:
    """Batch-load (text, subject) for the claim IDs we care about.

    We go through the storage layer — NOT raw SQL — to preserve the
    read-only contract and stay schema-agnostic.
    """
    if not ids:
        return {}
    from memorymaster.core.service import MemoryService

    svc = MemoryService(db_target=db_path, workspace_root=REPO)
    out: dict[int, tuple[str, str | None]] = {}
    for cid in ids:
        try:
            claim = svc.store.get_claim(cid, include_citations=False)
        except Exception:
            continue
        if claim is None:
            continue
        out[cid] = (
            getattr(claim, "text", "") or "",
            getattr(claim, "subject", None),
        )
    return out


def _evaluate_prompt(
    idx: int,
    rec: PromptRecord,
    db_path: str,
    ground_truth: dict[str, set[int]],
    min_overlap: int,
    claim_text_cache: dict[int, tuple[str, str | None]],
) -> PromptEval:
    """Run a single prompt through production recall() and score."""
    start = time.perf_counter()
    try:
        result = recall(
            rec.text,
            db_path=db_path,
            skip_qdrant=True,
            return_ids=True,
        )
    except Exception as exc:
        print(f"[{idx}] recall() raised: {exc}", file=sys.stderr)
        return PromptEval(
            idx=idx,
            prompt=rec.text[:120],
            sha=rec.sha,
            returned_ids=(),
            labels=(),
            p5=0.0,
            ap5=0.0,
            latency_ms=(time.perf_counter() - start) * 1000.0,
            label_source="error",
        )
    latency_ms = (time.perf_counter() - start) * 1000.0
    # When return_ids=True, recall returns (markdown, ids). Defensive coercion
    # because recall() legacy callers get just ``str`` — kept here so that
    # a typo in the kwarg won't silently blow up with a cryptic IndexError.
    if isinstance(result, tuple) and len(result) == 2:
        _rendered, returned_ids = result
    else:
        returned_ids = []

    # Prefer ground-truth labels when available for this prompt.
    gt_ids = ground_truth.get(rec.sha)
    if gt_ids is not None:
        labels = tuple(1 if cid in gt_ids else 0 for cid in returned_ids)
        label_source = "ground_truth"
    else:
        # Fall back to the heuristic token-overlap scorer. We need the
        # claim text to compute overlap — populate the cache lazily so we
        # don't pay for claims we never see.
        missing = [cid for cid in returned_ids if cid not in claim_text_cache]
        if missing:
            fetched = _lookup_claim_texts(db_path, missing)
            claim_text_cache.update(fetched)
        labels = tuple(
            _heuristic_relevance(
                claim_text_cache.get(cid, ("", None))[0],
                claim_text_cache.get(cid, ("", None))[1],
                rec.prompt_tokens,
                min_overlap=min_overlap,
            )
            for cid in returned_ids
        )
        label_source = "heuristic"

    return PromptEval(
        idx=idx,
        prompt=rec.text[:120],
        sha=rec.sha,
        returned_ids=tuple(returned_ids),
        labels=labels,
        p5=_precision_at_k(labels, k=5),
        ap5=_average_precision_at_k(labels, k=5),
        latency_ms=latency_ms,
        label_source=label_source,
    )


def run_eval(
    prompts: list[PromptRecord],
    db_path: str,
    ground_truth: dict[str, set[int]],
    min_overlap: int,
) -> list[PromptEval]:
    """Evaluate every prompt and return the per-prompt records."""
    claim_text_cache: dict[int, tuple[str, str | None]] = {}
    out: list[PromptEval] = []
    for i, rec in enumerate(prompts):
        out.append(_evaluate_prompt(
            idx=i,
            rec=rec,
            db_path=db_path,
            ground_truth=ground_truth,
            min_overlap=min_overlap,
            claim_text_cache=claim_text_cache,
        ))
    return out


def _summarize(results: list[PromptEval]) -> dict:
    n = max(1, len(results))
    p_sum = sum(r.p5 for r in results)
    m_sum = sum(r.ap5 for r in results)
    non_empty = sum(1 for r in results if r.returned_ids)
    hits = sum(1 for r in results if any(r.labels[:5]))
    lat_ms = [r.latency_ms for r in results]
    lat_ms.sort()
    p95_idx = int(0.95 * (len(lat_ms) - 1)) if lat_ms else 0
    gt_count = sum(1 for r in results if r.label_source == "ground_truth")
    return {
        "prompts_total": len(results),
        "precision_at_5": p_sum / n,
        "map_at_5": m_sum / n,
        "hit_at_5": hits / n,
        "non_empty": non_empty,
        "prompts_with_ground_truth": gt_count,
        "latency_ms_mean": (sum(lat_ms) / n) if lat_ms else 0.0,
        "latency_ms_p95": lat_ms[p95_idx] if lat_ms else 0.0,
    }


def _capture_env() -> dict[str, str]:
    return {k: os.environ[k] for k in _TRACKED_ENV if k in os.environ}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prompts", default="artifacts/real-prompts.jsonl")
    ap.add_argument("--db", default="memorymaster.db")
    ap.add_argument("--labels", default=None,
                    help="Path to labels side-file. Defaults to "
                         "<prompts-stem>-labels.json next to the prompts file.")
    ap.add_argument("--min-overlap", type=int, default=2,
                    help="Token-overlap threshold for heuristic relevance "
                         "(used only when a prompt has no ground-truth entry).")
    ap.add_argument("--label", default="prod-recall",
                    help="Short tag emitted in the summary line + JSON.")
    ap.add_argument("--json-out", default=None,
                    help="Write per-prompt records as JSONL, plus one "
                         "summary line at the end.")
    args = ap.parse_args()

    prompts_path = Path(args.prompts)
    if not prompts_path.is_absolute():
        prompts_path = REPO / prompts_path
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = REPO / db_path
    if not prompts_path.exists():
        print(f"ERROR: missing prompts file: {prompts_path}")
        return 2
    if not db_path.exists():
        print(f"ERROR: missing DB: {db_path}")
        return 2

    labels_path = Path(args.labels) if args.labels else prompts_path.with_name(
        prompts_path.stem + "-labels.json"
    )
    ground_truth, labels_min_overlap = _load_labels(labels_path)

    # Install read-only service shim ONCE before any recall() call.
    _patch_service_readonly()

    prompts = _load_prompts(prompts_path)
    effective_min_overlap = args.min_overlap
    print(f"[{args.label}] prompts={len(prompts)} "
          f"db={db_path.name} labels={'yes' if ground_truth else 'no'}"
          f" (have {len(ground_truth)} labeled / fall-back min_overlap="
          f"{effective_min_overlap})")

    env_snapshot = _capture_env()
    if env_snapshot:
        print(f"[{args.label}] env overrides: {env_snapshot}")

    start = time.perf_counter()
    results = run_eval(
        prompts=prompts,
        db_path=str(db_path),
        ground_truth=ground_truth,
        min_overlap=effective_min_overlap,
    )
    total_ms = (time.perf_counter() - start) * 1000.0

    summary = _summarize(results)
    summary.update({
        "label": args.label,
        "db": str(db_path),
        "prompts_file": str(prompts_path),
        "labels_file": str(labels_path) if labels_path.exists() else "",
        "labels_min_overlap": labels_min_overlap,
        "env": env_snapshot,
        "wall_ms_total": total_ms,
    })

    print(f"[{args.label}] precision@5  = {summary['precision_at_5']:.3f}")
    print(f"[{args.label}] MAP@5        = {summary['map_at_5']:.3f}")
    print(f"[{args.label}] hit@5        = {summary['hit_at_5']:.3f}")
    print(f"[{args.label}] non_empty    = {summary['non_empty']}/"
          f"{summary['prompts_total']}")
    print(f"[{args.label}] labeled GT   = "
          f"{summary['prompts_with_ground_truth']}/{summary['prompts_total']}")
    print(f"[{args.label}] latency mean = "
          f"{summary['latency_ms_mean']:.1f} ms, p95 = "
          f"{summary['latency_ms_p95']:.1f} ms")

    if args.json_out:
        out_path = Path(args.json_out)
        if not out_path.is_absolute():
            out_path = REPO / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            for r in results:
                fh.write(json.dumps({
                    "idx": r.idx,
                    "prompt": r.prompt,
                    "sha": r.sha,
                    "returned_ids": list(r.returned_ids),
                    "labels": list(r.labels),
                    "p5": r.p5,
                    "ap5": r.ap5,
                    "latency_ms": r.latency_ms,
                    "label_source": r.label_source,
                }) + "\n")
            fh.write(json.dumps({"__summary__": summary}) + "\n")
        print(f"[{args.label}] wrote {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
