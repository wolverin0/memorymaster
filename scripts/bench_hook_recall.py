"""Retrieval-only benchmark of the prompt-recall HOOK path (T-1862). No LLM is called.

For every question of a T-1817 manifest this calls ``context_hook.recall`` exactly as the
UserPromptSubmit hook does (``hook_data`` given: top-N claims, session-less, lexical path) and
scores the injected block with the same proxy as ``bench_transcript_fallback``:

* ``token_recall``: share of the gold answer's tokens found in the injected text
  (``answered`` = recall >= 0.5);
* ``hit``: a transcript fragment of the block lies in the evidence session and covers the
  evidence line (claims never carry a line range);
* hook latency in milliseconds (wall clock of the ``recall`` call: p50/p95).

Run it once per code version (``--label before`` on the base checkout, ``--label after`` on the
change) and compare the two JSON files.  The claims DB is only ever opened read-only; spool,
seen-state and hook-config locations are redirected into ``--state-dir`` so the operator's real
hook state is untouched.  Dense recall is switched off: this measures the lexical path.

``--scope oracle`` runs each question as an agent working in the project of its evidence session
(the transcript scope recorded in the sidecar); ``--scope fixed:<scope>`` pins one scope.
Each question runs twice: once as the hook really does (``raw``, timed) and once with the transcript
fragments that quote the benchmark itself removed (``clean``); report ``clean`` as the honest number.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _code_root() -> None:
    """Import ``memorymaster`` from ``--code-root`` (default: this checkout), never from a stale install."""
    root = Path(__file__).resolve().parents[1]
    for i, arg in enumerate(sys.argv):
        if arg == "--code-root" and i + 1 < len(sys.argv):
            root = Path(sys.argv[i + 1]).resolve()
    sys.path.insert(0, str(root))


_code_root()
from bench_transcript_fallback import _fix, _leaked_fragments, _percentile, _tokens  # noqa: E402


def _isolate(state_dir: Path) -> None:
    """Keep every write of the hook path (access spool, seen ids, config) out of the real home."""
    state_dir.mkdir(parents=True, exist_ok=True)
    for name in [k for k in os.environ if k.upper().startswith(("MEMORYMASTER_RECALL_DENSE", "MEMORYMASTER_DREAM"))]:
        del os.environ[name]
    os.environ["MEMORYMASTER_SPOOL_DIR"] = str(state_dir / "spool")
    os.environ["MEMORYMASTER_RECALL_SEEN_DIR"] = str(state_dir / "seen")
    os.environ["MEMORYMASTER_RECALL_HOOK_CONFIG"] = str(state_dir / "no-recall-hook.json")
    os.environ["MEMORYMASTER_RECALL_STATE_DIR"] = str(state_dir / "delivery")


def _session_scopes(index: Path) -> dict[str, str]:
    with closing(sqlite3.connect(f"file:{index.resolve().as_posix()}?mode=ro", uri=True)) as con:
        return dict(con.execute("SELECT session_id, scope FROM files"))


def _package_dir_name() -> str:
    import memorymaster

    return Path(memorymaster.__file__).resolve().parents[1].name


def _summary(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    return {
        "token_recall_mean": round(statistics.mean(r[key]["token_recall"] for r in rows), 3),
        "answered_at_0.5": round(sum(r[key]["answered"] for r in rows) / len(rows), 3),
        "evidence_hit": round(sum(r[key]["hit"] for r in rows) / len(rows), 3),
        "questions_with_fragments": sum(r[key]["fragments"] > 0 for r in rows),
        "mean_chars": round(statistics.mean(r[key]["chars"] for r in rows)),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    _isolate(args.state_dir)
    from memorymaster.recall import context_hook
    from memorymaster.recall import transcript_index as ti

    questions = json.loads(args.questions.read_text(encoding="utf-8"))["questions"]
    leaked = _leaked_fragments(args.index, questions)
    scopes = _session_scopes(args.index)
    served: list[Any] = []
    drop_leaks = [False]
    real_search = ti.search

    def search(query, index_path=None, **kw):
        """Record what the hook got; in the clean pass drop fragments that quote the benchmark itself."""
        if not drop_leaks[0]:
            hits = real_search(query, args.index, **kw)
        else:
            want = kw.get("limit", 5)
            deep = {**kw, "limit": max(want, 100), "deadline_ms": None}  # a deeper pool must not be cut by the hook's budget
            hits = [h for h in real_search(query, args.index, **deep)
                    if (h.session_id, h.first_line) not in leaked][:want]
        served.extend(hits)
        return hits

    ti.search = search
    rows: list[dict[str, Any]] = []
    for item in questions:
        query, gold = _fix(item["question"]), _tokens(_fix(item["answer"]))
        ev = item["evidence"]
        scope = args.scope.split(":", 1)[1] if args.scope.startswith("fixed:") else scopes.get(ev["session"], "project:unknown")
        os.environ["MEMORYMASTER_SCOPE_DEFAULT"] = scope
        row: dict[str, Any] = {"id": item["id"], "category": item["category"], "scope": scope}
        for key, clean in (("raw", False), ("clean", True)):
            drop_leaks[0] = clean
            ms = float("inf")
            for _ in range(args.passes if not clean else 1):  # best of N: robust to other load on the machine
                served.clear()
                start = time.perf_counter()
                text, ids = context_hook.recall(query, db_path=str(args.db), hook_data={"cwd": None}, return_ids=True)
                ms = min(ms, (time.perf_counter() - start) * 1000)
            recall = len(gold & _tokens(text)) / max(len(gold), 1)
            row[key] = {"claims": len(ids), "fragments": len(served), "chars": len(text), "token_recall": recall,
                        "answered": recall >= 0.5, "ms": ms,
                        "hit": any(h.session_id == ev["session"] and h.first_line <= ev["message_index"] <= h.last_line
                                   for h in served)}
        rows.append(row)
    ms = [r["raw"]["ms"] for r in rows]  # the unfiltered call is what the hook really does
    summary = {
        "label": args.label, "scope_mode": args.scope, "n": len(rows), "package": _package_dir_name(),
        "questions_with_claims": sum(r["raw"]["claims"] > 0 for r in rows),
        "raw_incl_benchmark_quoting_fragments": _summary(rows, "raw"),
        "clean_excluding_benchmark_quoting_fragments": _summary(rows, "clean"),
        "latency_ms_first": round(ms[0], 1),
        "latency_ms_p50": round(_percentile(ms, 0.5), 1),
        "latency_ms_p95": round(_percentile(ms, 0.95), 1),
        "latency_ms_p50_excl_first": round(_percentile(ms[1:], 0.5), 1),
    }
    return {"summary": summary, "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--code-root", type=Path, default=None, help="checkout whose memorymaster package is measured")
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("MEMORYMASTER_DEFAULT_DB", "memorymaster.db")),
                        help="claims DB (default: MEMORYMASTER_DEFAULT_DB; the service opens it read-only)")
    parser.add_argument("--index", type=Path, default=Path.home() / ".memorymaster" / "transcripts.db",
                        help="transcript sidecar index")
    parser.add_argument("--questions", type=Path, required=True, help="T-1817 questions.json")
    parser.add_argument("--state-dir", type=Path, required=True, help="scratch dir for spool / seen-state")
    parser.add_argument("--label", default="run")
    parser.add_argument("--passes", type=int, default=3, help="timed calls per question; the fastest is reported")
    parser.add_argument("--scope", default="oracle", help="oracle | fixed:<project scope>")
    parser.add_argument("--out", type=Path, default=None, help="write the per-question JSON here")
    args = parser.parse_args()
    result = run(args)
    if args.out:
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
