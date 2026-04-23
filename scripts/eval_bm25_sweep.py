"""Sweep BM25(k1, b) parameters against the 30-prompt precision@5 eval.

SQLite FTS5's built-in ``bm25()`` function exposes per-column weights but
NOT the k1/b hyperparameters — both are hard-coded at k1=1.2, b=0.75.
This harness therefore implements BM25 in Python:

1. Fetch candidate rowids per prompt using the existing FTS5 MATCH pipeline
   (same tokenizer, same fanout — so candidate recall is unchanged).
2. For each (k1, b) combo, recompute the BM25 score over the candidate
   set using stats derived from the claims table (read-only).
3. Inject the BM25 score as ``lexical_score`` and re-run the existing
   re-ranker (same weights as context_hook._relevance).
4. Record p@5, MAP@5, non-empty rate per combo. Pick the Pareto-optimal
   combo where p@5 is maximised without regressing MAP@5 or non-empty.

Read-only. No DB writes. No new deps.

Usage:
    python scripts/eval_bm25_sweep.py                                 # default grid
    python scripts/eval_bm25_sweep.py --db <path> --prompts <path>
    python scripts/eval_bm25_sweep.py --k1 1.6 --b 0.5                # single combo
    python scripts/eval_bm25_sweep.py --json-out artifacts/bm25.jsonl
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from memorymaster.recall_tokenizer import _candidate_tokens, extract_query_tokens  # noqa: E402
from memorymaster.service import MemoryService  # noqa: E402

# Re-rank weights shipped in context_hook.py::_RECALL_WEIGHT_DEFAULTS.
W0 = (0.3, 0.3, 0.2, 0.1, 0.1, 0.0, 0.0)

# Grid: 5 x 5 = 25 combos. k1 ∈ {0.8, 1.2, 1.6, 2.0, 2.5}; b ∈ {0.0, 0.25, 0.5, 0.75, 1.0}.
K1_VALUES = (0.8, 1.2, 1.6, 2.0, 2.5)
B_VALUES = (0.0, 0.25, 0.5, 0.75, 1.0)


@dataclass(frozen=True)
class CorpusStats:
    n_docs: int
    avg_doc_len: float
    df: dict[str, int]  # term -> document frequency (within the candidate union)


def _load_prompts(path: Path) -> list[str]:
    out: list[str] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            text = (rec.get("text") or "").strip()
            if text:
                out.append(text)
    return out


def _prompt_tokens(prompt: str) -> set[str]:
    return {t for t in _candidate_tokens(prompt) if len(t) >= 3}


def _claim_tokens(subject: str | None, text: str) -> list[str]:
    """Ordered token list (with repeats) for BM25 TF counting."""
    joined = f"{subject or ''} {text}"
    return [t for t in _candidate_tokens(joined) if len(t) >= 3]


def _label(prompt_tokens: set[str], claim_subject: str | None, claim_text: str,
           min_overlap: int = 2) -> int:
    ct = set(_claim_tokens(claim_subject, claim_text))
    return 1 if len(ct & prompt_tokens) >= min_overlap else 0


def _fetch_candidates(svc: MemoryService, prompt: str, db_path: str,
                      max_tokens: int = 6, top_k: int = 20) -> list[dict]:
    """Mirror context_hook.recall candidate fanout — identical semantics."""
    fts_query = extract_query_tokens(prompt, db_path, max_tokens=max_tokens)
    token_list = fts_query.split() if fts_query else []
    rows: list = []
    seen_ids: set[int] = set()
    if token_list:
        per_token_limit = max(5, (top_k * 2) // max(1, len(token_list)))
        for tok in token_list:
            batch = svc.query_rows(
                query_text=tok,
                limit=per_token_limit,
                retrieval_mode="legacy",
                include_candidates=True,
                scope_allowlist=None,
            )
            for row in batch:
                claim = row.get("claim")
                cid = getattr(claim, "id", None)
                if cid is None or cid in seen_ids:
                    continue
                seen_ids.add(cid)
                rows.append(row)
    if not rows:
        rows = svc.query_rows(
            query_text=prompt,
            limit=top_k,
            retrieval_mode="legacy",
            include_candidates=True,
            scope_allowlist=None,
        )
    for row in rows:
        row["_fts_query"] = fts_query
        row["_raw_query"] = prompt
    return rows


def _build_corpus_stats(collected: list[tuple[str, list[dict], set[str]]]) -> CorpusStats:
    """Build BM25 corpus stats from the candidate union.

    We compute stats over the CANDIDATE corpus (not the full DB) — this is
    the standard approach when re-ranking a retrieved shortlist, and it's
    also read-only-cheap. For the 30-prompt eval, the union is typically
    a few hundred distinct claims.
    """
    doc_lens: list[int] = []
    df: dict[str, int] = {}
    seen: set[int] = set()
    for _prompt, rows, _ptoks in collected:
        for row in rows:
            claim = row.get("claim")
            cid = getattr(claim, "id", None)
            if cid is None or cid in seen:
                continue
            seen.add(cid)
            subject = getattr(claim, "subject", "") or ""
            text = getattr(claim, "text", "") or ""
            tokens = _claim_tokens(subject, text)
            doc_lens.append(len(tokens))
            for t in set(tokens):
                df[t] = df.get(t, 0) + 1
    n = len(doc_lens)
    avg = (sum(doc_lens) / n) if n else 0.0
    return CorpusStats(n_docs=n, avg_doc_len=avg, df=df)


def _bm25(query_tokens: list[str], doc_tokens: list[str], stats: CorpusStats,
          k1: float, b: float) -> float:
    """Okapi BM25. Returns a non-negative score."""
    if not query_tokens or not doc_tokens or stats.n_docs == 0:
        return 0.0
    # Term frequency in doc.
    tf: dict[str, int] = {}
    for t in doc_tokens:
        tf[t] = tf.get(t, 0) + 1
    dl = len(doc_tokens)
    avgdl = stats.avg_doc_len or 1.0
    score = 0.0
    for q in query_tokens:
        f = tf.get(q, 0)
        if f == 0:
            continue
        n_q = stats.df.get(q, 0)
        # Robertson/Spärck Jones IDF (non-negative variant).
        # log((N - n + 0.5) / (n + 0.5) + 1) is monotone & always >= 0.
        idf = math.log(((stats.n_docs - n_q + 0.5) / (n_q + 0.5)) + 1.0)
        norm = 1.0 - b + b * (dl / avgdl)
        score += idf * ((f * (k1 + 1.0)) / (f + k1 * norm))
    return score


def _score_row(row: dict, weights: tuple[float, ...], bm25_val: float) -> float:
    """Replicate context_hook._relevance with bm25_val replacing lexical_score."""
    w_matches, w_phrase, w_all, w_lexical, w_confidence, w_freshness, w_vector = weights
    claim = row.get("claim")
    text = (claim.text if hasattr(claim, "text") else "").lower()
    fts_query = row.get("_fts_query", "") or ""
    raw_query = row.get("_raw_query", "") or ""
    query_words = set(fts_query.lower().split()) or set(raw_query.lower().split())
    tokens_gt2 = [w for w in query_words if len(w) > 2]
    matches = sum(1 for w in tokens_gt2 if w in text)
    phrase_bonus = 1.0 if raw_query and raw_query.lower() in text else 0.0
    all_present = 1.0 if tokens_gt2 and matches == len(tokens_gt2) else 0.0
    conf = float(row.get("confidence_score") or 0.0)
    freshness = float(row.get("freshness_score") or 0.0)
    vector = float(row.get("vector_score") or 0.0)
    return (
        matches * w_matches
        + phrase_bonus * w_phrase
        + all_present * w_all
        + bm25_val * w_lexical
        + conf * w_confidence
        + freshness * w_freshness
        + vector * w_vector
    )


def _precision_at_k(labels: list[int], k: int = 5) -> float:
    head = labels[:k]
    if not head:
        return 0.0
    return sum(head) / len(head)


def _map_at_k(labels: list[int], k: int = 5) -> float:
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


def _evaluate(collected: list[tuple[str, list[dict], set[str]]],
              stats: CorpusStats, k1: float, b: float,
              weights: tuple[float, ...] = W0, k: int = 5,
              min_overlap: int = 2) -> tuple[float, float, int]:
    """Return (precision@k mean, MAP@k mean, non-empty prompts)."""
    ps: list[float] = []
    aps: list[float] = []
    non_empty = 0
    for prompt, rows, ptoks in collected:
        if not rows:
            ps.append(0.0)
            aps.append(0.0)
            continue
        non_empty += 1
        q_tokens = list(_prompt_tokens(prompt))
        # Pre-compute doc-tokens once per doc (cheap, no DB hits).
        scored: list[tuple[float, dict, int]] = []
        for row in rows:
            claim = row.get("claim")
            subject = getattr(claim, "subject", "") or ""
            text = getattr(claim, "text", "") or ""
            doc_tokens = _claim_tokens(subject, text)
            bm = _bm25(q_tokens, doc_tokens, stats, k1, b)
            s = _score_row(row, weights, bm)
            scored.append((s, row, 0))
        scored.sort(key=lambda t: t[0], reverse=True)
        labels = [
            _label(ptoks, getattr(r.get("claim"), "subject", None),
                   getattr(r.get("claim"), "text", ""), min_overlap=min_overlap)
            for _s, r, _ in scored
        ]
        ps.append(_precision_at_k(labels, k))
        aps.append(_map_at_k(labels, k))
    n = max(1, len(collected))
    return sum(ps) / n, sum(aps) / n, non_empty


def _evaluate_default_lexical(collected: list[tuple[str, list[dict], set[str]]],
                               weights: tuple[float, ...] = W0, k: int = 5,
                               min_overlap: int = 2) -> tuple[float, float, int]:
    """Baseline: use the row's existing lexical_score (not BM25). Mirrors current prod."""
    ps: list[float] = []
    aps: list[float] = []
    non_empty = 0
    for _prompt, rows, ptoks in collected:
        if not rows:
            ps.append(0.0)
            aps.append(0.0)
            continue
        non_empty += 1
        scored: list[tuple[float, dict]] = []
        for row in rows:
            lex = float(row.get("lexical_score") or 0.0)
            s = _score_row(row, weights, lex)
            scored.append((s, row))
        scored.sort(key=lambda t: t[0], reverse=True)
        labels = [
            _label(ptoks, getattr(r.get("claim"), "subject", None),
                   getattr(r.get("claim"), "text", ""), min_overlap=min_overlap)
            for _s, r in scored
        ]
        ps.append(_precision_at_k(labels, k))
        aps.append(_map_at_k(labels, k))
    n = max(1, len(collected))
    return sum(ps) / n, sum(aps) / n, non_empty


def _collect(prompts: list[str], svc: MemoryService, db_path: str,
             top_k: int) -> list[tuple[str, list[dict], set[str]]]:
    out = []
    for prompt in prompts:
        rows = _fetch_candidates(svc, prompt, db_path, top_k=top_k)
        out.append((prompt, rows, _prompt_tokens(prompt)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prompts", default="artifacts/real-prompts.jsonl")
    ap.add_argument("--db", default="memorymaster.db")
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--min-overlap", type=int, default=2)
    ap.add_argument("--k1", type=float, default=None,
                    help="Single-point run: k1 value (requires --b).")
    ap.add_argument("--b", type=float, default=None,
                    help="Single-point run: b value (requires --k1).")
    ap.add_argument("--json-out", default=None,
                    help="Write grid results as JSONL.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    prompts_path = Path(args.prompts)
    if not prompts_path.is_absolute():
        prompts_path = REPO / prompts_path
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = REPO / db_path

    if not prompts_path.exists():
        print(f"ERROR: prompts not found: {prompts_path}")
        return 2
    if not db_path.exists():
        print(f"ERROR: db not found: {db_path}")
        return 2

    prompts = _load_prompts(prompts_path)
    svc = MemoryService(db_target=str(db_path), workspace_root=REPO)
    # Hard read-only — disable every write path the service could trigger.
    svc._record_accesses = lambda *a, **k: None  # type: ignore[assignment]
    if hasattr(svc, "store") and hasattr(svc.store, "record_accesses_batch"):
        svc.store.record_accesses_batch = lambda *a, **k: None  # type: ignore[assignment]

    print(f"Loading {len(prompts)} prompts from {prompts_path.name}")
    print(f"DB: {db_path.name} (read-only via _record_accesses override)")
    print(f"Fetching top-{args.top_k} candidates per prompt (one-shot)...")
    collected = _collect(prompts, svc, str(db_path), args.top_k)
    cand_counts = [len(r) for _, r, _ in collected]
    mean_cand = sum(cand_counts) / max(1, len(cand_counts))
    print(f"  mean candidates/prompt: {mean_cand:.1f} "
          f"(min={min(cand_counts, default=0)}, max={max(cand_counts, default=0)})")

    stats = _build_corpus_stats(collected)
    print(f"  corpus stats: n_docs={stats.n_docs}, avgdl={stats.avg_doc_len:.1f}, "
          f"vocab={len(stats.df)}")

    # Baseline: current lexical scorer (no BM25).
    p5_base, m5_base, ne_base = _evaluate_default_lexical(
        collected, weights=W0, min_overlap=args.min_overlap
    )
    print("\nBASELINE (current lexical_score, tokenizer v2):")
    print(f"  p@5={p5_base:.3f}  MAP@5={m5_base:.3f}  non-empty={ne_base}/{len(prompts)}")

    # Single-point run?
    if args.k1 is not None and args.b is not None:
        p5, m5, ne = _evaluate(collected, stats, args.k1, args.b,
                                weights=W0, min_overlap=args.min_overlap)
        print(f"\nBM25(k1={args.k1}, b={args.b}):")
        print(f"  p@5={p5:.3f}  (dlt vs baseline: {p5 - p5_base:+.3f})")
        print(f"  MAP@5={m5:.3f}  (dlt vs baseline: {m5 - m5_base:+.3f})")
        print(f"  non-empty={ne}/{len(prompts)}")
        return 0

    # Full grid.
    print(f"\nRunning grid: {len(K1_VALUES)}x{len(B_VALUES)} = {len(K1_VALUES)*len(B_VALUES)} combos...")
    results: list[dict] = []
    for k1 in K1_VALUES:
        for b in B_VALUES:
            p5, m5, ne = _evaluate(collected, stats, k1, b,
                                    weights=W0, min_overlap=args.min_overlap)
            results.append({"k1": k1, "b": b, "p5": p5, "m5": m5, "ne": ne})
            if args.verbose:
                print(f"  k1={k1:.2f}  b={b:.2f}  p@5={p5:.3f}  MAP@5={m5:.3f}  ne={ne}")

    # Sort by p@5 desc, MAP@5 desc.
    results.sort(key=lambda r: (r["p5"], r["m5"]), reverse=True)
    print("\nTop 10 (k1, b) by p@5 (tiebreak MAP@5):")
    for r in results[:10]:
        flag = (
            "WIN" if (r["p5"] - p5_base) >= 0.02 and (r["m5"] - m5_base) >= 0.01 and r["ne"] >= 28
            else "---"
        )
        print(f"  [{flag}] k1={r['k1']:.2f} b={r['b']:.2f}  "
              f"p@5={r['p5']:.3f} (dlt {r['p5']-p5_base:+.3f})  "
              f"MAP@5={r['m5']:.3f} (dlt {r['m5']-m5_base:+.3f})  "
              f"ne={r['ne']}")

    # Pareto winner: the best (k1, b) satisfying the ship criteria.
    winners = [
        r for r in results
        if (r["p5"] - p5_base) >= 0.02
        and (r["m5"] - m5_base) >= 0.01
        and r["ne"] >= 28
    ]
    print("\nPARETO VERDICT:")
    if winners:
        w = winners[0]
        print(f"  SHIP: k1={w['k1']} b={w['b']}  "
              f"p@5 {p5_base:.3f} -> {w['p5']:.3f} (+{w['p5']-p5_base:.3f})  "
              f"MAP@5 {m5_base:.3f} -> {w['m5']:.3f} (+{w['m5']-m5_base:.3f})  "
              f"ne {ne_base} -> {w['ne']}")
    else:
        best = results[0]
        print("  HOLD: best grid combo did not clear the ship gate.")
        print(f"  Best by p@5: k1={best['k1']} b={best['b']}  "
              f"p@5={best['p5']:.3f} (dlt {best['p5']-p5_base:+.3f})  "
              f"MAP@5={best['m5']:.3f} (dlt {best['m5']-m5_base:+.3f})  "
              f"ne={best['ne']}")
        print("  Current tokenizer v2 + overlap-lexical is near-optimal on this corpus.")

    if args.json_out:
        out_path = Path(args.json_out)
        if not out_path.is_absolute():
            out_path = REPO / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "baseline": {"p5": p5_base, "m5": m5_base, "ne": ne_base},
                "grid": results,
            }) + "\n")
        print(f"\n  wrote {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
