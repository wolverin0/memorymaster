"""Evaluate recall-hook ranking precision@5 and MAP@5 on real prompts.

Runs the recall pipeline end-to-end (query tokenization + per-token fanout
+ re-rank with configurable weight tuple), then scores the top-K against a
token-overlap proxy label.

Proxy label: a candidate is "relevant" to a prompt iff
``len(candidate_tokens & prompt_tokens) >= min_overlap`` after the same
stopword/stem filter used by the recall tokenizer. This is coarse but
reproducible and does not require manual annotations.

Usage:
    python scripts/eval_recall_precision_at_5.py                          # baseline w0
    python scripts/eval_recall_precision_at_5.py --weights 0.3,0.3,0.2,0.1,0.1,0.0,0.0
    python scripts/eval_recall_precision_at_5.py --grid                   # 5^5 grid search
    python scripts/eval_recall_precision_at_5.py --weights <w> --verbose

The weight tuple is 7-dim:
    (w_matches, w_phrase, w_all, w_lexical, w_confidence, w_freshness, w_vector)

Read-only against the live DB — monkey-patches _record_accesses to no-op.
"""
from __future__ import annotations

import argparse
import io
import itertools
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from memorymaster.recall_tokenizer import _candidate_tokens, extract_query_tokens  # noqa: E402
from memorymaster.service import MemoryService  # noqa: E402


# 8-dim weights — matches the order in context_hook._relevance.
# The 8th dim (w_entity) is shipped at 0.0 so legacy 7-dim invocations are
# equivalent to (..., w_entity=0.0). See context_hook._entity_fanout_claim_ids.
WEIGHT_NAMES = (
    "w_matches",
    "w_phrase",
    "w_all",
    "w_lexical",
    "w_confidence",
    "w_freshness",
    "w_vector",
    "w_entity",
)

# Baseline weights in context_hook.py::_relevance (matches + phrase + all + lex + conf).
# Freshness + vector + entity were unused in w0.
W0 = (0.3, 0.3, 0.2, 0.1, 0.1, 0.0, 0.0, 0.0)


@dataclass(frozen=True)
class PromptResult:
    prompt: str
    ranked_claim_ids: tuple[int, ...]
    labels: tuple[int, ...]  # 1 = relevant, 0 = not


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
    """Stopword-stripped tokens from a prompt — matches the recall tokenizer."""
    return {t for t in _candidate_tokens(prompt) if len(t) >= 3}


def _claim_tokens(subject: str | None, text: str) -> set[str]:
    """Stopword-stripped tokens from a claim's subject + text."""
    joined = f"{subject or ''} {text}"
    return {t for t in _candidate_tokens(joined) if len(t) >= 3}


def _label(prompt_tokens: set[str], claim_subject: str | None, claim_text: str,
           min_overlap: int = 2) -> int:
    """Proxy relevance label: 1 if token overlap >= min_overlap, else 0."""
    ct = _claim_tokens(claim_subject, claim_text)
    return 1 if len(ct & prompt_tokens) >= min_overlap else 0


def _fetch_candidates(svc: MemoryService, prompt: str, db_path: str,
                      max_tokens: int = 6, top_k: int = 20,
                      include_entity_fanout: bool = True,
                      include_vector_fallback: bool = False) -> list[dict]:
    """Mirror context_hook.recall() candidate collection, but fetch top_k.

    When ``include_entity_fanout`` is True, also mines entities from the
    prompt and unions in claims linked by ``entity_id`` (lower-weight
    via ``entity_score=1.0``). This matches context_hook.recall when
    ``MEMORYMASTER_RECALL_W_ENTITY > 0``.

    When ``include_vector_fallback`` is True AND the post-fanout candidate
    count is below :func:`memorymaster.qdrant_recall_fallback.fallback_threshold`,
    also unions in Qdrant semantic-search hits. Env vars must be set as in
    context_hook.recall (MEMORYMASTER_QDRANT_URL + MEMORYMASTER_RECALL_VECTOR_FALLBACK).
    """
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
            claim = row.get("claim")
            cid = getattr(claim, "id", None)
            if cid is not None:
                seen_ids.add(cid)
    if include_entity_fanout:
        from memorymaster.context_hook import (
            _entity_fanout_claim_ids,
            _row_for_claim,
        )
        fanout_ids = _entity_fanout_claim_ids(svc.store, prompt, seen_ids)
        for cid in fanout_ids:
            try:
                claim = svc.store.get_claim(cid, include_citations=True)
            except Exception:
                continue
            if claim is None or getattr(claim, "status", "") == "archived":
                continue
            rows.append(_row_for_claim(claim))
    # Vector fallback stream (Qdrant + sentence-transformers), gated by
    # MEMORYMASTER_RECALL_VECTOR_FALLBACK=1. Runs only when the primary
    # FTS5+entity candidate pool is below the configured threshold.
    if include_vector_fallback:
        from memorymaster import qdrant_recall_fallback
        from memorymaster.context_hook import _row_for_vector_hit
        if (
            qdrant_recall_fallback.is_fallback_enabled()
            and len(rows) < qdrant_recall_fallback.fallback_threshold()
        ):
            try:
                hits = qdrant_recall_fallback.search(prompt)
            except Exception:
                hits = []
            for hit in hits:
                if hit.claim_id in seen_ids:
                    continue
                try:
                    claim = svc.store.get_claim(hit.claim_id, include_citations=True)
                except Exception:
                    continue
                if claim is None or getattr(claim, "status", "") == "archived":
                    continue
                rows.append(_row_for_vector_hit(claim, hit.score))
                seen_ids.add(hit.claim_id)
    # Verbatim stream — gated on MEMORYMASTER_RECALL_VERBATIM=1. Mirrors the
    # boost-or-inject strategy in context_hook.recall so the eval harness
    # agrees with the production hook.
    try:
        from memorymaster.verbatim_recall import (
            hit_to_synthetic_row,
            is_enabled as _verbatim_enabled,
            recall_verbatim,
        )
    except Exception:
        _verbatim_enabled = lambda: False  # noqa: E731
        recall_verbatim = lambda *a, **k: []  # noqa: E731
        hit_to_synthetic_row = None
    if _verbatim_enabled():
        try:
            verbatim_hits = recall_verbatim(prompt, scope=None,
                                            db_path=db_path, limit=5)
        except Exception:
            verbatim_hits = []
        if verbatim_hits and hit_to_synthetic_row is not None:
            scope_to_rows: dict[str, list[dict]] = {}
            for row in rows:
                claim = row.get("claim")
                s = getattr(claim, "scope", "") or ""
                if s:
                    scope_to_rows.setdefault(s, []).append(row)
            added_keys: set[str] = set()
            for hit in verbatim_hits:
                existing = scope_to_rows.get(hit.scope) or []
                if existing:
                    target = existing[0]
                    prev = float(target.get("verbatim_score") or 0.0)
                    if hit.score > prev:
                        target["verbatim_score"] = hit.score
                    continue
                key = hit.excerpt[:100]
                if key in added_keys:
                    continue
                added_keys.add(key)
                rows.append(hit_to_synthetic_row(hit))
    # Attach the tokenized query for re-ranking.
    for row in rows:
        row["_fts_query"] = fts_query
        row["_raw_query"] = prompt
    return rows


def _score(row: dict, weights: tuple[float, ...]) -> float:
    """Replicate context_hook._relevance with injected weights."""
    # Pad legacy 7-tuples to 9-dim for backwards compat.
    if len(weights) == 7:
        weights = (*weights, 0.0, 0.0)
    if len(weights) == 8:
        # Pad w_verbatim=env-value when caller didn't specify.
        from memorymaster.verbatim_recall import verbatim_weight
        weights = (*weights, verbatim_weight())
    (w_matches, w_phrase, w_all, w_lexical, w_confidence,
     w_freshness, w_vector, w_entity, w_verbatim) = weights
    claim = row.get("claim")
    text = (claim.text if hasattr(claim, "text") else "").lower()
    fts_query = row.get("_fts_query", "") or ""
    raw_query = row.get("_raw_query", "") or ""
    query_words = set(fts_query.lower().split()) or set(raw_query.lower().split())
    tokens_gt2 = [w for w in query_words if len(w) > 2]
    matches = sum(1 for w in tokens_gt2 if w in text)
    phrase_bonus = 1.0 if raw_query and raw_query.lower() in text else 0.0
    all_present = 1.0 if tokens_gt2 and matches == len(tokens_gt2) else 0.0
    lexical = float(row.get("lexical_score") or 0.0)
    conf = float(row.get("confidence_score") or 0.0)
    freshness = float(row.get("freshness_score") or 0.0)
    vector = float(row.get("vector_score") or 0.0)
    entity = float(row.get("entity_score") or 0.0)
    verbatim = float(row.get("verbatim_score") or 0.0)
    return (
        matches * w_matches
        + phrase_bonus * w_phrase
        + all_present * w_all
        + lexical * w_lexical
        + conf * w_confidence
        + freshness * w_freshness
        + vector * w_vector
        + entity * w_entity
        + verbatim * w_verbatim
    )


def _rank(rows: list[dict], weights: tuple[float, ...]) -> list[dict]:
    return sorted(rows, key=lambda r: _score(r, weights), reverse=True)


def _precision_at_k(labels: list[int], k: int = 5) -> float:
    head = labels[:k]
    if not head:
        return 0.0
    return sum(head) / len(head)


def _average_precision_at_k(labels: list[int], k: int = 5) -> float:
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


def _collect_candidates(prompts: list[str], svc: MemoryService, db_path: str,
                        top_k: int = 20,
                        include_entity_fanout: bool = True,
                        include_vector_fallback: bool = False) -> list[tuple[str, list[dict], set[str]]]:
    """Fetch top_k candidates once per prompt — reused across weight tuples."""
    out = []
    for prompt in prompts:
        rows = _fetch_candidates(svc, prompt, db_path, top_k=top_k,
                                 include_entity_fanout=include_entity_fanout,
                                 include_vector_fallback=include_vector_fallback)
        ptoks = _prompt_tokens(prompt)
        out.append((prompt, rows, ptoks))
    return out


def _evaluate(collected: list[tuple[str, list[dict], set[str]]],
              weights: tuple[float, ...], k: int = 5,
              min_overlap: int = 2) -> tuple[float, float, int]:
    """Return (precision@k mean, MAP@k mean, prompts-with-any-hit)."""
    ps, aps = [], []
    hit_prompts = 0
    for _prompt, rows, ptoks in collected:
        if not rows:
            ps.append(0.0)
            aps.append(0.0)
            continue
        ranked = _rank(rows, weights)
        labels = [
            _label(ptoks, getattr(r.get("claim"), "subject", None),
                   getattr(r.get("claim"), "text", ""), min_overlap=min_overlap)
            for r in ranked
        ]
        ps.append(_precision_at_k(labels, k))
        aps.append(_average_precision_at_k(labels, k))
        if any(labels[:k]):
            hit_prompts += 1
    n = max(1, len(collected))
    return sum(ps) / n, sum(aps) / n, hit_prompts


def _vector_fallback_env_on() -> bool:
    """True when BOTH env gates are set — mirrors context_hook behaviour."""
    import os as _os
    flag = _os.environ.get("MEMORYMASTER_RECALL_VECTOR_FALLBACK", "").strip()
    url = _os.environ.get("MEMORYMASTER_QDRANT_URL", "").strip()
    return bool(url) and flag.lower() not in ("", "0", "false", "no", "off")


def _parse_weights(s: str) -> tuple[float, ...]:
    parts = [float(x) for x in s.split(",")]
    if len(parts) == 7:
        parts.append(0.0)  # pad w_entity=0.0 for legacy invocations
    if len(parts) != 8:
        raise argparse.ArgumentTypeError(
            f"Expected 7 or 8 comma-separated weights ({','.join(WEIGHT_NAMES)}), got {len(parts)}"
        )
    return tuple(parts)


def _grid_iter(values: tuple[float, ...]) -> itertools.product:
    """5-dim grid (first 5 weights) — freshness/vector held at 0 for w0, varied for tune."""
    return itertools.product(values, repeat=5)


def _extended_grid_iter(values: tuple[float, ...]) -> itertools.product:
    """7-dim extended grid used when the 5-dim one plateaus."""
    return itertools.product(values, repeat=7)


def run_grid_search(collected, values: tuple[float, ...] = (0.0, 0.1, 0.2, 0.3, 0.5),
                    include_freshness_vector: bool = True,
                    min_overlap: int = 2,
                    verbose: bool = False) -> list[tuple[tuple[float, ...], float, float]]:
    """Return list of (weights, precision@5, MAP@5) sorted by p@5 desc."""
    results = []
    if include_freshness_vector:
        # 8-dim grid: 4^8 = 65536. Narrow values for speed.
        vals_8 = (0.0, 0.15, 0.3, 0.5)
        iterator = itertools.product(vals_8, repeat=8)
        pad_to_8 = False
    else:
        iterator = itertools.product(values, repeat=5)
        pad_to_8 = True
    count = 0
    for w in iterator:
        if pad_to_8:
            w = (*w, 0.0, 0.0, 0.0)
        if all(x == 0.0 for x in w):
            continue
        p5, m5, _hits = _evaluate(collected, w, min_overlap=min_overlap)
        results.append((w, p5, m5))
        count += 1
        if verbose and count % 500 == 0:
            best = max(results, key=lambda r: (r[1], r[2]))
            print(f"  [grid] {count} tried  best p@5={best[1]:.3f}  w={best[0]}")
    results.sort(key=lambda r: (r[1], r[2]), reverse=True)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prompts", default="artifacts/real-prompts.jsonl")
    ap.add_argument("--db", default="memorymaster.db")
    ap.add_argument("--weights", type=_parse_weights, default=None,
                    help="7-dim comma-separated weight tuple")
    ap.add_argument("--grid", action="store_true", help="Run full grid search")
    ap.add_argument("--grid-narrow", action="store_true",
                    help="Run narrow 5-dim grid (freshness/vector=0)")
    ap.add_argument("--top-k", type=int, default=20,
                    help="Fetch top-K candidates per prompt before re-ranking")
    ap.add_argument("--min-overlap", type=int, default=2,
                    help="Token-overlap threshold for proxy relevance label")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--no-entity-fanout", action="store_true",
                    help="Disable entity-link fanout (reproduce pre-fanout baseline)")
    ap.add_argument("--vector-fallback", action="store_true",
                    help="Enable Qdrant vector-search fallback (also honours "
                         "MEMORYMASTER_RECALL_VECTOR_FALLBACK + QDRANT_URL env).")
    ap.add_argument("--json-out", default=None, help="Write grid search results as JSONL")
    args = ap.parse_args()

    prompts_path = Path(args.prompts)
    if not prompts_path.is_absolute():
        prompts_path = REPO / prompts_path
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = REPO / db_path

    if not prompts_path.exists() or not db_path.exists():
        missing = prompts_path if not prompts_path.exists() else db_path
        print(f"ERROR: missing {missing}")
        return 2

    prompts = _load_prompts(prompts_path)
    svc = MemoryService(db_target=str(db_path), workspace_root=REPO)
    # Hard read-only: disable every write that the service could trigger.
    svc._record_accesses = lambda *a, **k: None  # type: ignore[assignment]
    if hasattr(svc, "store") and hasattr(svc.store, "record_accesses_batch"):
        svc.store.record_accesses_batch = lambda *a, **k: None  # type: ignore[assignment]

    print(f"Loading {len(prompts)} prompts from {prompts_path.name}")
    print(f"DB: {db_path.name} (read-only via _record_accesses override)")
    fanout = not args.no_entity_fanout
    print(f"Entity-link fanout: {'ON' if fanout else 'OFF'}")
    vector_fallback = args.vector_fallback or _vector_fallback_env_on()
    print(f"Vector fallback:    {'ON' if vector_fallback else 'OFF'}")
    print(f"Fetching top-{args.top_k} candidates per prompt (one-shot, reused across grid)...")
    collected = _collect_candidates(prompts, svc, str(db_path), top_k=args.top_k,
                                     include_entity_fanout=fanout,
                                     include_vector_fallback=vector_fallback)
    cand_counts = [len(r) for _, r, _ in collected]
    print(f"  mean candidates/prompt: {sum(cand_counts) / max(1, len(cand_counts)):.1f} "
          f"(min={min(cand_counts, default=0)}, max={max(cand_counts, default=0)})")

    # Baseline
    p5_base, m5_base, hits_base = _evaluate(collected, W0, min_overlap=args.min_overlap)
    print(f"\nBASELINE weights w0 = {W0}   (min_overlap={args.min_overlap})")
    print(f"  precision@5 = {p5_base:.3f}")
    print(f"  MAP@5       = {m5_base:.3f}")
    print(f"  prompts with >=1 hit in top-5 = {hits_base}/{len(prompts)}")

    if args.weights:
        p5, m5, hits = _evaluate(collected, args.weights, min_overlap=args.min_overlap)
        print(f"\nCANDIDATE weights = {args.weights}")
        print(f"  precision@5 = {p5:.3f}  (delta vs baseline: {p5 - p5_base:+.3f})")
        print(f"  MAP@5       = {m5:.3f}  (delta vs baseline: {m5 - m5_base:+.3f})")
        print(f"  prompts with >=1 hit in top-5 = {hits}/{len(prompts)}")

    if args.grid or args.grid_narrow:
        include_fv = not args.grid_narrow
        print(f"\nRunning grid search (include_freshness_vector={include_fv})...")
        results = run_grid_search(collected, include_freshness_vector=include_fv,
                                  min_overlap=args.min_overlap, verbose=args.verbose)
        print(f"  tried {len(results)} weight tuples")
        print("\nTop 10 weight tuples by precision@5 (tiebreak MAP@5):")
        for w, p5, m5 in results[:10]:
            print(f"  p@5={p5:.3f}  MAP@5={m5:.3f}  w={w}")
        best_w, best_p5, best_m5 = results[0]
        print(f"\nGRID WINNER: {best_w}")
        print(f"  precision@5 = {best_p5:.3f}  (delta vs baseline: {best_p5 - p5_base:+.3f})")
        print(f"  MAP@5       = {best_m5:.3f}  (delta vs baseline: {best_m5 - m5_base:+.3f})")
        ship_ok = (best_p5 - p5_base) >= 0.05
        print(f"  ship? {'YES' if ship_ok else 'NO  (improvement <0.05 — current near-optimal)'}")
        acceptance = best_p5 >= 0.70
        print(f"  acceptance bar (>=0.70): {'PASS' if acceptance else 'FAIL'}")

        if args.json_out:
            out_path = Path(args.json_out)
            if not out_path.is_absolute():
                out_path = REPO / out_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", encoding="utf-8") as fh:
                for w, p5, m5 in results:
                    fh.write(json.dumps({"w": list(w), "p5": p5, "m5": m5}) + "\n")
            print(f"  wrote grid results: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
