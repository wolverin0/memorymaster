"""Macro-F1 evaluation of the MemoryMaster classify hook.

The classify hook lives at ~/.claude/hooks/memorymaster-classify.py — a
regex-based multi-label classifier that tags user prompts with routing
signals: DECISION, BUG_ROOT_CAUSE, GOTCHA, CONSTRAINT, ARCHITECTURE,
ENVIRONMENT, REFERENCE.

This harness loads the hook as a module, runs it against a labeled fixture
of real prompts, and reports per-class precision/recall/F1 plus macro-F1.

Usage:
    python scripts/eval_classify_f1.py
    python scripts/eval_classify_f1.py --fixture tests/fixtures/classify_eval.jsonl
    python scripts/eval_classify_f1.py --hook path/to/memorymaster-classify.py
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_HOOK = Path(os.path.expanduser("~")) / ".claude" / "hooks" / "memorymaster-classify.py"
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "classify_eval.jsonl"

LABEL_SET = (
    "DECISION",
    "BUG_ROOT_CAUSE",
    "GOTCHA",
    "CONSTRAINT",
    "ARCHITECTURE",
    "ENVIRONMENT",
    "REFERENCE",
)


@dataclass(frozen=True)
class LabeledCase:
    prompt: str
    labels: frozenset


def load_fixture(path: Path) -> list[LabeledCase]:
    cases: list[LabeledCase] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            cases.append(LabeledCase(prompt=d["prompt"], labels=frozenset(d.get("labels", []))))
    return cases


def load_classify_fn(hook_path: Path):
    """Load `classify` from the hook script as a module."""
    spec = importlib.util.spec_from_file_location("classify_hook", hook_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load hook module from {hook_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.classify


def _counts(predictions: list[frozenset], golds: list[frozenset], label: str) -> tuple[int, int, int]:
    tp = fp = fn = 0
    for pred, gold in zip(predictions, golds):
        in_pred = label in pred
        in_gold = label in gold
        if in_pred and in_gold:
            tp += 1
        elif in_pred and not in_gold:
            fp += 1
        elif not in_pred and in_gold:
            fn += 1
    return tp, fp, fn


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f


def evaluate(
    cases: Iterable[LabeledCase],
    classify_fn,
) -> dict:
    cases = list(cases)
    predictions: list[frozenset] = []
    for case in cases:
        signals = classify_fn(case.prompt)
        # classify returns list of (name, message) tuples
        predictions.append(frozenset(name for name, _ in signals))
    golds = [c.labels for c in cases]

    per_class: dict[str, dict] = {}
    f1s: list[float] = []
    for label in LABEL_SET:
        tp, fp, fn = _counts(predictions, golds, label)
        p, r, f = _prf(tp, fp, fn)
        per_class[label] = {
            "tp": tp, "fp": fp, "fn": fn,
            "precision": round(p, 4),
            "recall": round(r, 4),
            "f1": round(f, 4),
            "support": sum(1 for g in golds if label in g),
        }
        f1s.append(f)
    macro_f1 = sum(f1s) / len(f1s) if f1s else 0.0

    # Exact-match accuracy (all labels exactly right)
    exact = sum(1 for p, g in zip(predictions, golds) if p == g)

    return {
        "n_cases": len(cases),
        "macro_f1": round(macro_f1, 4),
        "exact_match": exact,
        "exact_match_rate": round(exact / len(cases), 4) if cases else 0.0,
        "per_class": per_class,
        "predictions": [sorted(p) for p in predictions],
        "golds": [sorted(g) for g in golds],
    }


def render_report(result: dict) -> str:
    lines = [
        f"# Classify hook eval — {result['n_cases']} cases",
        "",
        f"**Macro-F1:** {result['macro_f1']:.4f}",
        f"**Exact match:** {result['exact_match']}/{result['n_cases']} "
        f"({result['exact_match_rate']:.2%})",
        "",
        "## Per-class",
        "",
        "| Class | Support | TP | FP | FN | Precision | Recall | F1 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for label in LABEL_SET:
        m = result["per_class"][label]
        lines.append(
            f"| {label} | {m['support']} | {m['tp']} | {m['fp']} | {m['fn']} | "
            f"{m['precision']:.4f} | {m['recall']:.4f} | {m['f1']:.4f} |"
        )
    return "\n".join(lines) + "\n"


def render_mistakes(cases: list[LabeledCase], result: dict, limit: int = 50) -> str:
    lines = ["", "## Mistakes (up to {n})".format(n=limit), ""]
    shown = 0
    for i, case in enumerate(cases):
        if shown >= limit:
            break
        pred = set(result["predictions"][i])
        gold = set(result["golds"][i])
        if pred == gold:
            continue
        missed = gold - pred
        extra = pred - gold
        snippet = case.prompt.replace("\n", " ")[:120]
        lines.append(f"- [{i}] gold={sorted(gold)} pred={sorted(pred)} "
                     f"missed={sorted(missed)} extra={sorted(extra)}")
        lines.append(f"      \"{snippet}\"")
        shown += 1
    return "\n".join(lines) + "\n"


def main() -> int:
    # Force UTF-8 stdout on Windows (cp1252 chokes on ❯, —, etc.)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    ap.add_argument("--hook", type=Path, default=DEFAULT_HOOK)
    ap.add_argument("--out-report", type=Path, default=None,
                    help="If set, also write markdown report to this path.")
    ap.add_argument("--json", action="store_true",
                    help="Print per-class results as JSON (for scripting).")
    ap.add_argument("--show-mistakes", action="store_true",
                    help="Include per-prompt mistakes section in report.")
    args = ap.parse_args()

    if not args.fixture.exists():
        print(f"error: fixture not found: {args.fixture}", file=sys.stderr)
        return 2
    if not args.hook.exists():
        print(f"error: hook not found: {args.hook}", file=sys.stderr)
        return 2

    classify_fn = load_classify_fn(args.hook)
    cases = load_fixture(args.fixture)
    result = evaluate(cases, classify_fn)

    report = render_report(result)
    if args.show_mistakes:
        report += render_mistakes(cases, result)
    print(report)

    if args.out_report is not None:
        args.out_report.parent.mkdir(parents=True, exist_ok=True)
        args.out_report.write_text(report, encoding="utf-8")
        print(f"[report] wrote {args.out_report}")

    if args.json:
        print(json.dumps({
            "n_cases": result["n_cases"],
            "macro_f1": result["macro_f1"],
            "exact_match_rate": result["exact_match_rate"],
            "per_class": result["per_class"],
        }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
