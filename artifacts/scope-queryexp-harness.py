"""Companion harness for roadmap 1.2 + 1.5 evaluation.

The main ``scripts/eval_recall_precision_at_5.py`` harness is read-only per
the task spec. Its ``_score()`` function duplicates the ranker formula
**without** the scope-boost multiplier, and its ``_fetch_candidates()``
duplicates the FTS5 fanout **without** the query-expansion wiring. Both
features therefore invisible to the shipped eval — the baseline is reported
identically across all 4 configs, which is useful as a regression guard but
doesn't measure the features.

This harness calls ``context_hook.recall()`` directly so the real production
code path is exercised end-to-end — scope boost, query expansion, BM25
rescore, everything. It parses the rendered Memory Context output and maps
each bullet back to its claim via a secondary SQL lookup, then applies the
same token-overlap proxy label as the shipped eval for apples-to-apples
p@5 / MAP@5 / non_empty metrics.

Usage::

    python artifacts/scope-queryexp-harness.py \
        --prompts artifacts/real-prompts-100.jsonl \
        --db /path/to/memorymaster.db \
        --scope-boost 0.1 \
        --query-expansion 1
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from memorymaster.context_hook import recall  # noqa: E402
from memorymaster.recall_tokenizer import _candidate_tokens  # noqa: E402


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


def _claim_tokens(claim_text: str) -> set[str]:
    return {t for t in _candidate_tokens(claim_text) if len(t) >= 3}


def _label(ptoks: set[str], claim_text: str, min_overlap: int = 2) -> int:
    return 1 if len(_claim_tokens(claim_text) & ptoks) >= min_overlap else 0


def _precision_at_k(labels: list[int], k: int = 5) -> float:
    head = labels[:k]
    return sum(head) / len(head) if head else 0.0


def _ap_at_k(labels: list[int], k: int = 5) -> float:
    head = labels[:k]
    if not head:
        return 0.0
    hits = 0
    total = 0.0
    for i, lab in enumerate(head, 1):
        if lab:
            hits += 1
            total += hits / i
    return total / max(1, sum(head))


def _parse_recall_bullets(rendered: str) -> list[str]:
    out: list[str] = []
    for line in rendered.splitlines():
        if line.startswith("- "):
            body = line[2:].strip()
            # Trim the wiki-link suffix: "text  (compiled in [[slug]])"
            if "  (compiled in [[" in body:
                body = body.split("  (compiled in [[", 1)[0]
            out.append(body)
    return out


def _evaluate(prompts: list[str], db_path: str, *, k: int = 5,
              min_overlap: int = 2) -> dict:
    ps, aps = [], []
    non_empty = 0
    per_prompt: list[dict] = []
    for i, prompt in enumerate(prompts):
        try:
            rendered = recall(prompt, db_path=db_path, skip_qdrant=True)
        except Exception as exc:
            rendered = ""
            print(f"[{i}] recall() raised: {exc}", file=sys.stderr)
        bullets = _parse_recall_bullets(rendered)
        if bullets:
            non_empty += 1
        ptoks = _prompt_tokens(prompt)
        labels = [_label(ptoks, b, min_overlap=min_overlap) for b in bullets]
        p5 = _precision_at_k(labels, k)
        m5 = _ap_at_k(labels, k)
        ps.append(p5)
        aps.append(m5)
        per_prompt.append({
            "idx": i,
            "prompt": prompt[:120],
            "n_bullets": len(bullets),
            "p5": p5,
            "ap5": m5,
            "labels_top5": labels[:k],
        })
    n = max(1, len(prompts))
    return {
        "prompts_total": len(prompts),
        "precision_at_5": sum(ps) / n,
        "map_at_5": sum(aps) / n,
        "non_empty": non_empty,
        "per_prompt": per_prompt,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prompts", default="artifacts/real-prompts-100.jsonl")
    ap.add_argument("--db", required=True,
                    help="Path to live memorymaster.db (read-only usage)")
    ap.add_argument("--scope-boost", default=None,
                    help="Sets MEMORYMASTER_RECALL_SCOPE_BOOST for this run")
    ap.add_argument("--query-expansion", default=None,
                    help="Sets MEMORYMASTER_RECALL_QUERY_EXPANSION for this run")
    ap.add_argument("--scope-default", default=None,
                    help="Sets MEMORYMASTER_SCOPE_DEFAULT for this run")
    ap.add_argument("--min-overlap", type=int, default=2)
    ap.add_argument("--label", default="cfg",
                    help="Label emitted in the summary line")
    ap.add_argument("--json-out", default=None,
                    help="Write per-prompt detail as JSONL")
    args = ap.parse_args()

    if args.scope_boost is not None:
        os.environ["MEMORYMASTER_RECALL_SCOPE_BOOST"] = args.scope_boost
    else:
        os.environ.pop("MEMORYMASTER_RECALL_SCOPE_BOOST", None)
    if args.query_expansion is not None:
        os.environ["MEMORYMASTER_RECALL_QUERY_EXPANSION"] = args.query_expansion
    else:
        os.environ.pop("MEMORYMASTER_RECALL_QUERY_EXPANSION", None)
    if args.scope_default is not None:
        os.environ["MEMORYMASTER_SCOPE_DEFAULT"] = args.scope_default

    # Monkey-patch MemoryService to avoid writing _record_accesses on a 7 GB DB.
    from memorymaster import service as _svc_mod
    _original_init = _svc_mod.MemoryService.__init__

    def _ro_init(self, *a, **kw):
        _original_init(self, *a, **kw)
        # Replace with no-op so read-only semantics hold.
        self._record_accesses = lambda *a, **k: None
        if hasattr(self.store, "record_accesses_batch"):
            self.store.record_accesses_batch = lambda *a, **k: None

    _svc_mod.MemoryService.__init__ = _ro_init

    prompts_path = Path(args.prompts)
    if not prompts_path.is_absolute():
        prompts_path = REPO / prompts_path

    prompts = _load_prompts(prompts_path)
    print(f"[{args.label}] prompts={len(prompts)} db={args.db}")
    print(f"[{args.label}] SCOPE_BOOST={os.environ.get('MEMORYMASTER_RECALL_SCOPE_BOOST', 'unset')} "
          f"QUERY_EXPANSION={os.environ.get('MEMORYMASTER_RECALL_QUERY_EXPANSION', 'unset')}")

    result = _evaluate(prompts, args.db, min_overlap=args.min_overlap)

    print(f"[{args.label}] p@5  = {result['precision_at_5']:.3f}")
    print(f"[{args.label}] MAP@5= {result['map_at_5']:.3f}")
    print(f"[{args.label}] non_empty = {result['non_empty']}/{result['prompts_total']}")

    if args.json_out:
        out_path = Path(args.json_out)
        if not out_path.is_absolute():
            out_path = REPO / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            for row in result["per_prompt"]:
                fh.write(json.dumps(row) + "\n")
        print(f"[{args.label}] wrote {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
