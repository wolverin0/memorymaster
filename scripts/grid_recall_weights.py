"""Grid-search the recall weight knobs against precision@5.

Sweeps W_LEXICAL × W_FRESHNESS × W_GRAPH against the existing 100-prompt
evaluation harness (`scripts/eval_recall_precision_at_5.py`) and writes a
sorted markdown table + raw JSONL log so a future tweak is reproducible.

W_VECTOR is skipped because the local DB has no Qdrant; the stream is a
no-op without `MEMORYMASTER_USE_QDRANT=1` and a populated index.

Usage:
    python scripts/grid_recall_weights.py \
        --prompts artifacts/real-prompts-100.jsonl \
        --db memorymaster.db \
        --output artifacts/recall-weight-tuning-2026-04-26.md
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Modest 3 × 3 × 4 = 36 grid. Bounded by ~10s/combo wall via subprocess startup.
W_LEXICAL_GRID = (0.2, 0.3, 0.4)
W_FRESHNESS_GRID = (0.0, 0.05, 0.1)
W_GRAPH_GRID = (0.0, 0.05, 0.1, 0.2)

METRIC_RE = {
    "precision@5": re.compile(r"precision@5\s*=\s*([\d.]+)"),
    "MAP@5": re.compile(r"MAP@5\s*=\s*([\d.]+)"),
    "hit@5": re.compile(r"hit@5\s*=\s*([\d.]+)"),
    "p95_ms": re.compile(r"p95\s*=\s*([\d.]+)\s*ms"),
}


def _run_eval(
    eval_script: Path,
    prompts: Path,
    db: Path,
    weights: dict,
    json_out: Path,
    label: str,
) -> dict | None:
    env = os.environ.copy()
    for k, v in weights.items():
        env[f"MEMORYMASTER_RECALL_{k}"] = str(v)
    # The GRAPH stream is opt-in: W_GRAPH alone is a no-op unless the stream
    # itself is enabled. Turn it on only when the weight is non-zero — keeps
    # the latency-cost cells out of the grid when they can't possibly help.
    if weights.get("W_GRAPH", 0) > 0:
        env["MEMORYMASTER_RECALL_GRAPH"] = "1"
    # Same for the freshness stream.
    if weights.get("W_FRESHNESS", 0) > 0:
        env["MEMORYMASTER_RECALL_FRESHNESS"] = "1"

    proc = subprocess.run(
        [
            sys.executable,
            str(eval_script),
            "--prompts",
            str(prompts),
            "--db",
            str(db),
            "--json-out",
            str(json_out),
            "--label",
            label,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    if proc.returncode != 0:
        return {"error": proc.stderr.strip()[:200] or "non-zero exit"}

    out = proc.stdout
    parsed = {"label": label, **{k: v for k, v in weights.items()}}
    for metric, rgx in METRIC_RE.items():
        m = rgx.search(out)
        parsed[metric] = float(m.group(1)) if m else None
    return parsed


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prompts", type=Path, required=True)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument(
        "--eval-script",
        type=Path,
        default=Path("scripts/eval_recall_precision_at_5.py"),
    )
    p.add_argument(
        "--per-run-json-dir",
        type=Path,
        default=Path("artifacts/grid-runs"),
        help="Per-cell raw eval JSONL dump directory.",
    )
    args = p.parse_args()

    args.per_run_json_dir.mkdir(parents=True, exist_ok=True)

    combos = list(
        itertools.product(W_LEXICAL_GRID, W_FRESHNESS_GRID, W_GRAPH_GRID)
    )
    print(f"[grid] running {len(combos)} cells over W_LEXICAL × W_FRESHNESS × W_GRAPH")

    rows: list[dict] = []
    t_total = time.monotonic()
    for i, (w_lex, w_fresh, w_graph) in enumerate(combos, 1):
        weights = {"W_LEXICAL": w_lex, "W_FRESHNESS": w_fresh, "W_GRAPH": w_graph}
        label = f"L{w_lex}_F{w_fresh}_G{w_graph}"
        json_out = args.per_run_json_dir / f"{label}.jsonl"
        t0 = time.monotonic()
        row = _run_eval(args.eval_script, args.prompts, args.db, weights, json_out, label)
        wall = time.monotonic() - t0
        if row is None:
            row = {"error": "no output"}
        row["wall_s"] = round(wall, 1)
        rows.append(row)
        prec = row.get("precision@5")
        prec_str = f"{prec:.3f}" if isinstance(prec, float) else "ERR"
        print(f"[grid] {i}/{len(combos)} {label} wall={wall:.1f}s p@5={prec_str}")

    # Pick best by precision@5 (tie-break MAP@5 desc, then p95 asc)
    valid = [r for r in rows if isinstance(r.get("precision@5"), float)]
    valid.sort(
        key=lambda r: (
            -r["precision@5"],
            -(r.get("MAP@5") or 0.0),
            r.get("p95_ms") or 1e9,
        )
    )

    # Write markdown report
    lines = [
        "# Recall weight grid — precision@5 tuning",
        "",
        f"- Eval prompts: `{args.prompts}` (100, 70 labeled)",
        f"- DB: `{args.db}` (post-L2-backfill snapshot)",
        f"- Grid: W_LEXICAL × W_FRESHNESS × W_GRAPH = "
        f"{len(W_LEXICAL_GRID)} × {len(W_FRESHNESS_GRID)} × {len(W_GRAPH_GRID)} = {len(combos)} cells",
        f"- Total wall: {round(time.monotonic()-t_total, 1)}s",
        "",
        "## Top 10 by precision@5",
        "",
        "| W_LEXICAL | W_FRESHNESS | W_GRAPH | precision@5 | MAP@5 | hit@5 | p95 ms | wall s |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in valid[:10]:
        lines.append(
            f"| {r['W_LEXICAL']} | {r['W_FRESHNESS']} | {r['W_GRAPH']} "
            f"| {r['precision@5']:.3f} | {r.get('MAP@5'):.3f} | {r.get('hit@5'):.3f} "
            f"| {r.get('p95_ms')} | {r['wall_s']} |"
        )
    if not valid:
        lines.append("| — | — | — | NO VALID RUNS | | | | |")
    else:
        winner = valid[0]
        lines += [
            "",
            "## Winner",
            "",
            f"`MEMORYMASTER_RECALL_W_LEXICAL={winner['W_LEXICAL']}` "
            f"`MEMORYMASTER_RECALL_W_FRESHNESS={winner['W_FRESHNESS']}` "
            f"`MEMORYMASTER_RECALL_W_GRAPH={winner['W_GRAPH']}`",
            "",
            f"precision@5 = **{winner['precision@5']:.3f}** "
            f"(baseline 0.152, delta = {(winner['precision@5'] - 0.152):+.3f})",
        ]

    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Also dump rows as JSON for downstream automation
    json_path = args.output.with_suffix(".json")
    json_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    print(f"[grid] wrote {args.output}")
    print(f"[grid] wrote {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
