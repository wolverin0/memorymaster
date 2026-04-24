"""One-off eval harness for roadmap 1.4 BM25 per-field weighting.

The shipped ``scripts/eval_recall_precision_at_5.py`` has its own inline
``_score`` implementation that reads ``row["lexical_score"]`` (the FTS5 rank
from retrieval) — it does NOT exercise the BM25 rescorer that lives inside
``context_hook.recall``. That's fine for the per-weight grid search it was
built for, but it makes it impossible to measure a BM25-internal change
(like per-field weighting) through that script.

This harness reuses the same candidate-collection path, then applies the
EXACT per-field BM25 rescorer from ``memorymaster.context_hook`` so each
config is a true end-to-end measurement. It writes ``row["lexical_score"]``
back with the per-field score before delegating to the eval's ``_evaluate``
helper so the rest of the pipeline (ranker weights, labels, p@5, MAP@5) is
identical across configs.

Run::

    python artifacts/bm25-per-field-eval-harness.py [--prompts ...] [--db ...]

Does NOT modify the DB. Read-only, like the parent eval.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path

# Add repo root and scripts/ to path.
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

# Import from the eval script (treat it as a module).
import importlib.util
spec = importlib.util.spec_from_file_location(
    "eval_module", REPO / "scripts" / "eval_recall_precision_at_5.py"
)
assert spec is not None and spec.loader is not None
eval_module = importlib.util.module_from_spec(spec)
sys.modules["eval_module"] = eval_module  # dataclass needs cls.__module__ resolvable
spec.loader.exec_module(eval_module)

from memorymaster.context_hook import (
    _BM25_K1_DEFAULT,
    _BM25_B_DEFAULT,
    _BM25_W_SUBJECT_DEFAULT,
    _BM25_W_TEXT_DEFAULT,
)
from memorymaster.recall_tokenizer import _candidate_tokens
from memorymaster.service import MemoryService


def _tokens(raw: str) -> list[str]:
    if not isinstance(raw, str):
        return []
    return [t for t in _candidate_tokens(raw) if len(t) >= 3]


def _apply_per_field_bm25(
    prompt: str,
    rows: list[dict],
    w_subject: float,
    w_text: float,
    k1: float = _BM25_K1_DEFAULT,
    b: float = _BM25_B_DEFAULT,
) -> None:
    """Overwrite ``row["lexical_score"]`` with per-field BM25 for each row.

    This replicates the logic in context_hook.recall() so the eval
    harness measures the same scoring code as production.
    """
    # Per-field tokenisation + df.
    subj_tok: dict[int, list[str]] = {}
    text_tok: dict[int, list[str]] = {}
    df_s: dict[str, int] = {}
    df_t: dict[str, int] = {}
    for r in rows:
        c = r.get("claim")
        cid = getattr(c, "id", None)
        if cid is None or cid in subj_tok:
            continue
        st = _tokens(getattr(c, "subject", "") or "")
        tt = _tokens(getattr(c, "text", "") or "")
        subj_tok[cid] = st
        text_tok[cid] = tt
        for t in set(st):
            df_s[t] = df_s.get(t, 0) + 1
        for t in set(tt):
            df_t[t] = df_t.get(t, 0) + 1

    n_docs = len(subj_tok)
    non_empty_s = [v for v in subj_tok.values() if v]
    non_empty_t = [v for v in text_tok.values() if v]
    avg_s = sum(len(v) for v in non_empty_s) / len(non_empty_s) if non_empty_s else 0.0
    avg_t = sum(len(v) for v in non_empty_t) / len(non_empty_t) if non_empty_t else 0.0

    q_tokens = [t for t in _candidate_tokens(prompt) if len(t) >= 3]

    def field_score(toks: list[str], df: dict[str, int], avg: float) -> float:
        if not toks or avg <= 0.0:
            return 0.0
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        dl = len(toks)
        s = 0.0
        for qt in q_tokens:
            f = tf.get(qt, 0)
            if f == 0:
                continue
            n_q = df.get(qt, 0)
            idf = math.log(((n_docs - n_q + 0.5) / (n_q + 0.5)) + 1.0)
            norm = 1.0 - b + b * (dl / avg)
            s += idf * ((f * (k1 + 1.0)) / (f + k1 * norm))
        return s

    # Write per-field combined score into row["lexical_score"] so the
    # downstream eval _score() sees it as the lexical signal. This is the
    # one mutation; everything else is untouched.
    scores: dict[int, float] = {}
    if n_docs > 0 and q_tokens:
        for cid in subj_tok:
            ss = field_score(subj_tok[cid], df_s, avg_s)
            ts = field_score(text_tok[cid], df_t, avg_t)
            scores[cid] = w_subject * ss + w_text * ts

    for r in rows:
        c = r.get("claim")
        cid = getattr(c, "id", None)
        if cid is not None and cid in scores:
            r["lexical_score"] = scores[cid]
        else:
            r["lexical_score"] = 0.0


def _apply_concat_bm25(
    prompt: str,
    rows: list[dict],
    k1: float = _BM25_K1_DEFAULT,
    b: float = _BM25_B_DEFAULT,
) -> None:
    """Replicate the pre-change concatenated BM25 scorer for an honest baseline.

    Mirrors the block at context_hook.py commit 3a34b2d:529-582.
    """
    tok: dict[int, list[str]] = {}
    df: dict[str, int] = {}
    for r in rows:
        c = r.get("claim")
        cid = getattr(c, "id", None)
        if cid is None or cid in tok:
            continue
        subject = getattr(c, "subject", "") or ""
        text = getattr(c, "text", "") or ""
        if not isinstance(subject, str):
            subject = ""
        if not isinstance(text, str):
            text = ""
        joined = f"{subject} {text}"
        toks = [t for t in _candidate_tokens(joined) if len(t) >= 3]
        tok[cid] = toks
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    n_docs = len(tok)
    avg = sum(len(v) for v in tok.values()) / n_docs if n_docs else 0.0
    q_tokens = [t for t in _candidate_tokens(prompt) if len(t) >= 3]
    scores: dict[int, float] = {}
    if n_docs > 0 and avg > 0 and q_tokens:
        for cid, toks in tok.items():
            if not toks:
                continue
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            dl = len(toks)
            s = 0.0
            for qt in q_tokens:
                f = tf.get(qt, 0)
                if f == 0:
                    continue
                n_q = df.get(qt, 0)
                idf = math.log(((n_docs - n_q + 0.5) / (n_q + 0.5)) + 1.0)
                norm = 1.0 - b + b * (dl / avg)
                s += idf * ((f * (k1 + 1.0)) / (f + k1 * norm))
            scores[cid] = s
    for r in rows:
        c = r.get("claim")
        cid = getattr(c, "id", None)
        if cid is not None and cid in scores:
            r["lexical_score"] = scores[cid]
        else:
            r["lexical_score"] = 0.0


def run_config(
    collected: list[tuple[str, list[dict], object]],
    label: str,
    rescorer,
    *rescorer_args,
    min_overlap: int = 2,
) -> tuple[float, float, int]:
    # Fresh copies per config (rescorer mutates lexical_score).
    import copy
    rescored = []
    for prompt, rows, svc_tokens in collected:
        fresh = [dict(r) for r in rows]
        rescorer(prompt, fresh, *rescorer_args)
        rescored.append((prompt, fresh, svc_tokens))
    p5, m5, hits = eval_module._evaluate(
        rescored, eval_module.W0, min_overlap=min_overlap
    )
    return p5, m5, hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--prompts",
        default=str(REPO.parent.parent.parent / "artifacts" / "real-prompts.jsonl"),
    )
    ap.add_argument(
        "--db",
        default=str(REPO.parent.parent.parent / "memorymaster.db"),
    )
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--min-overlap", type=int, default=2)
    args = ap.parse_args()

    prompts_path = Path(args.prompts)
    db_path = Path(args.db)
    if not prompts_path.exists() or not db_path.exists():
        print(f"ERROR missing: prompts={prompts_path}  db={db_path}")
        return 2

    prompts = eval_module._load_prompts(prompts_path)
    svc = MemoryService(db_target=str(db_path), workspace_root=REPO)
    svc._record_accesses = lambda *a, **k: None  # type: ignore[assignment]
    if hasattr(svc, "store") and hasattr(svc.store, "record_accesses_batch"):
        svc.store.record_accesses_batch = lambda *a, **k: None  # type: ignore[assignment]

    print(f"Loaded {len(prompts)} prompts, collecting top-{args.top_k} candidates...")
    collected = eval_module._collect_candidates(
        prompts, svc, str(db_path), top_k=args.top_k,
        include_entity_fanout=True, include_vector_fallback=False,
    )
    cand_counts = [len(r) for _, r, _ in collected]
    print(f"  mean candidates/prompt: {sum(cand_counts) / max(1, len(cand_counts)):.1f} "
          f"(min={min(cand_counts, default=0)}, max={max(cand_counts, default=0)})")

    configs = [
        ("A concat baseline            ", _apply_concat_bm25, ()),
        ("B per-field W_S=2.0 W_T=1.0  ", _apply_per_field_bm25, (2.0, 1.0)),
        ("C per-field W_S=3.0 W_T=1.0  ", _apply_per_field_bm25, (3.0, 1.0)),
        ("D per-field W_S=1.5 W_T=1.0  ", _apply_per_field_bm25, (1.5, 1.0)),
        ("E per-field W_S=5.0 W_T=1.0  ", _apply_per_field_bm25, (5.0, 1.0)),
        ("F per-field W_S=10.0 W_T=0.0 ", _apply_per_field_bm25, (10.0, 0.0)),
        ("G per-field W_S=0.0 W_T=10.0 ", _apply_per_field_bm25, (0.0, 10.0)),
        ("H per-field W_S=1.0 W_T=1.0  ", _apply_per_field_bm25, (1.0, 1.0)),
    ]

    print("\n{:<34} {:>10} {:>10} {:>12}".format(
        "config", "p@5", "MAP@5", "non_empty"))
    print("-" * 70)
    results = []
    for label, fn, args_tuple in configs:
        p5, m5, hits = run_config(collected, label, fn, *args_tuple,
                                  min_overlap=args.min_overlap)
        print(f"{label}  {p5:>8.3f}  {m5:>8.3f}   {hits:>3}/{len(prompts)}")
        results.append((label, p5, m5, hits))

    # Sample drill-down: find a prompt where concat (A) and per-field
    # H=(1.0, 1.0) give a DIFFERENT top-1, and print both top-5 lists.
    for prompt, rows, _ in collected:
        if len(rows) < 5:
            continue
        rows_concat = [dict(r) for r in rows]
        rows_pf = [dict(r) for r in rows]
        _apply_concat_bm25(prompt, rows_concat)
        _apply_per_field_bm25(prompt, rows_pf, 1.0, 1.0)
        # Rank by the hook's real _relevance proxy (W0).
        top5_concat = eval_module._rank(rows_concat, eval_module.W0)[:5]
        top5_pf = eval_module._rank(rows_pf, eval_module.W0)[:5]
        id0_c = getattr(top5_concat[0].get("claim"), "id", None)
        id0_p = getattr(top5_pf[0].get("claim"), "id", None)
        if id0_c != id0_p:
            print("\n--- sample prompt where top-1 differs ---")
            print(f"PROMPT: {prompt[:120]!r}")
            print("concat baseline top-5:")
            for row in top5_concat:
                c = row.get("claim")
                print(f"  cid={getattr(c, 'id', '?')!s:>6}  "
                      f"subj={str(getattr(c, 'subject', ''))[:40]!r}  "
                      f"text={str(getattr(c, 'text', ''))[:70]!r}")
            print("per-field (1.0, 1.0) top-5:")
            for row in top5_pf:
                c = row.get("claim")
                print(f"  cid={getattr(c, 'id', '?')!s:>6}  "
                      f"subj={str(getattr(c, 'subject', ''))[:40]!r}  "
                      f"text={str(getattr(c, 'text', ''))[:70]!r}")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
