"""Retrieval-only benchmark: claims alone vs claims + raw-transcript fallback (T-1860).

No LLM is called.  For every question of a T-1817 manifest the script retrieves
context with each arm and scores it with two proxies against the gold answer:

* ``token_recall``: share of the gold answer's tokens (``[A-Za-z0-9_-]{4,}``,
  lower-cased) found in the retrieved text; ``answered`` = recall >= 0.5.
* ``hit``: a retrieved fragment lies in the evidence session and covers the
  evidence message line (only fragments carry a line range; claims never hit).

Arms: ``claims`` (top-5 claims), ``fallback`` (top-5 transcript fragments),
``union5`` (claims and fragments interleaved, 5 items: T-1817's union budget),
``claims+fallback`` (claims top-5 then fragments top-5: the real fallback use).

The claims DB is opened through ``MemoryService(read_only=True)`` and refused
otherwise; the transcript index is a sidecar built beforehand with
``memorymaster transcript-index``.  Latency is wall-clock per retrieval call.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from memorymaster.recall import transcript_index as ti

TOP_K = 5
_TOK = re.compile(r"[A-Za-z0-9_-]{4,}")


def _fix(text: str) -> str:
    """Undo the double UTF-8 encoding found in the T-1817 manifest."""
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOK.findall(text)}


def _claims(db: Path, workspace: Path, query: str) -> tuple[list[str], float]:
    from memorymaster.core.service import MemoryService

    start = time.perf_counter()
    service = MemoryService(db, workspace_root=workspace, read_only=True)
    if not getattr(service.store, "read_only", False):
        raise RuntimeError("refusing non-read-only claim store")
    rows = service.query_rows(query, limit=TOP_K, retrieval_mode="legacy", record_accesses=False)
    texts = [str(getattr(row.get("claim"), "text", "")) for row in rows[:TOP_K]]
    return texts, (time.perf_counter() - start) * 1000


_LEAK_MARKERS = ("answer only from the supplied context", "judge whether the proposed answer", "evidence_sid")


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _leaked_fragments(index: Path, questions: list[dict[str, Any]]) -> set[tuple[str, int]]:
    """Fragments that quote this benchmark (prompts, judge calls, manifest dumps).

    The T-1817 harness ran its answer/judge calls as Claude sessions, so their
    transcripts contain every question and gold answer verbatim.  A fragment
    that holds a question, a gold-answer prefix or a harness marker is not
    independent evidence, so it is excluded from the fallback arm, except the
    fragment that covers a question's own evidence line.
    """
    needles = {n for q in questions for n in (_norm(_fix(q["question"]))[:60], _norm(_fix(q["answer"]))[:50]) if n}
    evidence = [(q["evidence"]["session"], q["evidence"]["message_index"]) for q in questions]
    leaked: set[tuple[str, int]] = set()
    with closing(sqlite3.connect(f"file:{index.resolve().as_posix()}?mode=ro", uri=True)) as con:
        for sid, first, last, text in con.execute("SELECT session_id, first_line, last_line, text FROM fragments"):
            if any(sid == s and first <= i <= last for s, i in evidence):
                continue
            flat = _norm(text)
            if any(m in flat for m in _LEAK_MARKERS) or any(n in flat for n in needles):
                leaked.add((sid, first))
    return leaked


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(pct * (len(ordered) - 1))))]


def run(args: argparse.Namespace) -> dict[str, Any]:
    questions = json.loads(args.questions.read_text(encoding="utf-8"))["questions"]
    rows: list[dict[str, Any]] = []
    leaked = _leaked_fragments(args.index, questions) if args.exclude_leaks else set()
    for item in questions:
        query, gold = _fix(item["question"]), _tokens(_fix(item["answer"]))
        claim_texts, claim_ms = _claims(args.db, args.workspace, query)
        start = time.perf_counter()
        shipped = ti.search(query, args.index, limit=TOP_K)  # exactly what the CLI does: timed
        frag_ms = (time.perf_counter() - start) * 1000
        # Quality uses a deeper pool so leak-filtering can still leave TOP_K independent hits.
        hits = [h for h in ti.search(query, args.index, limit=100) if (h.session_id, h.first_line) not in leaked][:TOP_K]
        del shipped
        ev = item["evidence"]
        claim_items = [(t, False) for t in claim_texts]
        frag_items = [
            (h.excerpt, h.session_id == ev["session"] and h.first_line <= ev["message_index"] <= h.last_line)
            for h in hits
        ]
        union5 = [x for pair in zip(claim_items, frag_items) for x in pair]
        union5 += claim_items[len(frag_items):] + frag_items[len(claim_items):]
        contexts = {
            "claims": claim_items,
            "fallback": frag_items,
            "union5": union5[:TOP_K],
            "claims+fallback": claim_items + frag_items,
        }
        for arm, items in contexts.items():
            recall = len(gold & _tokens(" ".join(t for t, _ in items))) / max(len(gold), 1)
            ms = {"claims": claim_ms, "fallback": frag_ms}.get(arm, claim_ms + frag_ms)
            hit = any(flag for _, flag in items)
            rows.append({"id": item["id"], "category": item["category"], "arm": arm,
                         "token_recall": recall, "answered": recall >= 0.5, "hit": hit, "ms": ms})
    summary = {}
    for arm in ("claims", "fallback", "union5", "claims+fallback"):
        group = [r for r in rows if r["arm"] == arm]
        ms = [r["ms"] for r in group]
        summary[arm] = {
            "n": len(group),
            "token_recall_mean": round(statistics.mean(r["token_recall"] for r in group), 3),
            "answered_at_0.5": round(sum(r["answered"] for r in group) / len(group), 3),
            "evidence_hit": round(sum(r["hit"] for r in group) / len(group), 3),
            "latency_ms_p50": round(_percentile(ms, 0.5), 1),
            "latency_ms_p95": round(_percentile(ms, 0.95), 1),
        }
    return {"summary": summary, "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, required=True, help="claims DB (opened read-only)")
    parser.add_argument("--index", type=Path, required=True, help="transcript sidecar index")
    parser.add_argument("--questions", type=Path, required=True, help="T-1817 questions.json")
    parser.add_argument("--no-exclude-leaks", dest="exclude_leaks", action="store_false",
                        help="keep fragments that quote the benchmark itself (inflates the result)")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, default=None, help="write full per-question JSON here")
    args = parser.parse_args()
    result = run(args)
    if args.out:
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
