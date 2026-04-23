"""Regression gate on classify-hook macro-F1.

Runs the classify hook from ``memorymaster/config_templates/hooks`` against
the labeled fixture and asserts macro-F1 stays above a floor. If you edit
the regex patterns, update this fixture and the floor together.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "memorymaster" / "config_templates" / "hooks" / "memorymaster-classify.py"
FIXTURE = ROOT / "tests" / "fixtures" / "classify_eval.jsonl"

LABELS = (
    "DECISION", "BUG_ROOT_CAUSE", "GOTCHA", "CONSTRAINT",
    "ARCHITECTURE", "ENVIRONMENT", "REFERENCE",
)

# Floor: set just below the measured macro-F1 to catch regressions while
# leaving a small wiggle room for future labeled prompts.
MACRO_F1_FLOOR = 0.90
PER_CLASS_F1_FLOOR = 0.75  # any class dropping below this fails


def _load_classify():
    spec = importlib.util.spec_from_file_location("classify_hook", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.classify


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f


@pytest.fixture(scope="module")
def eval_result():
    classify = _load_classify()
    cases = []
    with FIXTURE.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            cases.append(json.loads(line))
    per_class = {}
    f1s = []
    for label in LABELS:
        tp = fp = fn = 0
        for c in cases:
            gold = label in c.get("labels", [])
            pred = label in {n for n, _ in classify(c["prompt"])}
            if pred and gold:
                tp += 1
            elif pred and not gold:
                fp += 1
            elif gold and not pred:
                fn += 1
        p, r, f = _prf(tp, fp, fn)
        per_class[label] = f
        f1s.append(f)
    return {"macro_f1": sum(f1s) / len(f1s), "per_class": per_class, "n": len(cases)}


def test_macro_f1_floor(eval_result):
    assert eval_result["macro_f1"] >= MACRO_F1_FLOOR, (
        f"Macro-F1 dropped to {eval_result['macro_f1']:.4f} "
        f"(floor {MACRO_F1_FLOOR}); update regex or lower floor."
    )


def test_per_class_floor(eval_result):
    offenders = {
        label: f for label, f in eval_result["per_class"].items()
        if f < PER_CLASS_F1_FLOOR
    }
    assert not offenders, f"Per-class F1 below floor: {offenders}"
