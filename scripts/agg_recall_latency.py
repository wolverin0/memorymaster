#!/usr/bin/env python3
"""Aggregate recall-hook latency samples (roadmap 5.1).

Reads a ``hook.log`` produced by ``memorymaster.hook_log.log_hook`` and prints
an operator-friendly p50/p99/mean/count table per retrieval stream.

Usage:
    python scripts/agg_recall_latency.py <path/to/hook.log>
    python scripts/agg_recall_latency.py  # defaults to ~/.memorymaster/hook_state/hook.log

Stdlib-only. Does not mutate the log. Silently ignores malformed lines so a
corrupt tail never hides good data earlier in the file.

Log line format (from ``memorymaster.hook_log.log_hook``)::

    [HH:MM:SS] hook=recall event=latency stream=fts5 ms=12.345
    [HH:MM:SS] hook=recall event=latency_total total_ms=42.1 fts5_ms=12.3 ...

We ONLY read ``event=latency`` lines for per-stream stats (the
``latency_total`` row is a redundant snapshot — including it would double-
count). ``total`` is built from ``latency_total / total_ms``.
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
from pathlib import Path

# Match a single key=value token. Values may or may not be quoted.
# We don't reparse the whole envelope — we just pull the fields we need.
_KV = re.compile(r"(\w+)=(?:\"([^\"]*)\"|(\S+))")


def _default_log_path() -> Path:
    """Where :func:`log_hook` writes on Windows and POSIX."""
    return Path(os.path.expanduser("~")) / ".memorymaster" / "hook_state" / "hook.log"


def _parse_fields(line: str) -> dict[str, str]:
    """Return a dict of the k=v tokens in one log line. Keys with empty
    values are kept as empty strings — callers decide how to handle them."""
    out: dict[str, str] = {}
    for m in _KV.finditer(line):
        key = m.group(1)
        quoted = m.group(2)
        bare = m.group(3)
        out[key] = quoted if quoted is not None else (bare or "")
    return out


def _safe_float(raw: str) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def load_samples(path: Path) -> dict[str, list[float]]:
    """Return ``{stream_name: [ms, ...]}`` harvested from the log.

    Uses per-stream lines (``event=latency`` with ``stream=<name>`` and
    ``ms=<float>``) AND the consolidated ``event=latency_total`` line to
    pick up the ``total`` aggregate (there is no per-call ``stream=total``
    line — total is emitted once per call as ``total_ms``).
    """
    samples: dict[str, list[float]] = {}
    if not path.exists():
        return samples

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            line = raw_line.rstrip("\n")
            if "hook=recall" not in line:
                continue
            fields = _parse_fields(line)
            event = fields.get("event", "")
            if event == "latency":
                stream = fields.get("stream")
                ms = _safe_float(fields.get("ms", ""))
                if stream and ms is not None:
                    samples.setdefault(stream, []).append(ms)
            elif event == "latency_total":
                ms = _safe_float(fields.get("total_ms", ""))
                if ms is not None:
                    samples.setdefault("total", []).append(ms)
    return samples


def percentile(values: list[float], pct: float) -> float:
    """Closest-rank p50/p99 for small samples. Returns 0.0 on empty input.

    We use closest-rank (not linear interpolation) because a sample with
    only 2 points and p99 should just be "the bigger one" — interpolation
    would hide the tail we care about. For large samples the difference is
    negligible (sub-millisecond).
    """
    if not values:
        return 0.0
    s = sorted(values)
    # closest-rank: ceil(pct * N) index (1-based → subtract 1)
    rank = max(1, min(len(s), math.ceil(pct * len(s) / 100.0)))
    return s[rank - 1]


def mean(values: list[float]) -> float:
    return (sum(values) / len(values)) if values else 0.0


# Canonical stream order for the printed table — matches the pipeline order
# inside recall() so operators read it top-to-bottom as "what happens per
# call". Streams not in this list are appended alphabetically at the end.
_STREAM_ORDER = [
    "fts5",
    "entity_fanout",
    "bm25_rescore",
    "vector_fallback",
    "verbatim",
    "rank_and_build",
    "total",
]


def _ordered_streams(samples: dict[str, list[float]]) -> list[str]:
    known = [s for s in _STREAM_ORDER if s in samples]
    extras = sorted(s for s in samples if s not in _STREAM_ORDER)
    return known + extras


def format_table(samples: dict[str, list[float]]) -> str:
    """Render a monospace table: stream | N | p50 | p99 | mean (all ms)."""
    header = ("stream", "N", "p50_ms", "p99_ms", "mean_ms")
    rows: list[tuple[str, str, str, str, str]] = [header]
    for stream in _ordered_streams(samples):
        values = samples[stream]
        rows.append(
            (
                stream,
                str(len(values)),
                f"{percentile(values, 50):.2f}",
                f"{percentile(values, 99):.2f}",
                f"{mean(values):.2f}",
            )
        )

    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    lines: list[str] = []
    for idx, r in enumerate(rows):
        lines.append("  ".join(r[i].ljust(widths[i]) for i in range(len(header))))
        if idx == 0:
            lines.append("  ".join("-" * widths[i] for i in range(len(header))))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Aggregate recall latency samples from hook.log",
    )
    ap.add_argument(
        "log_path",
        nargs="?",
        default=str(_default_log_path()),
        help="Path to hook.log (default: ~/.memorymaster/hook_state/hook.log)",
    )
    args = ap.parse_args(argv)

    log_path = Path(args.log_path).expanduser()
    samples = load_samples(log_path)

    if not samples:
        print(f"[agg_recall_latency] no recall latency samples found in {log_path}")
        return 1

    print(f"recall latency aggregated from {log_path}")
    print(f"total calls (from latency_total): {len(samples.get('total', []))}")
    print()
    print(format_table(samples))
    return 0


if __name__ == "__main__":
    sys.exit(main())
