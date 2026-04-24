"""Train the isotonic-/sigmoid-calibrated steward promotion classifier.

Reads ``tests/fixtures/steward_training.jsonl`` (from
``build_steward_training_set.py``), splits it (``--split`` chooses strategy),
fits ``LogisticRegression(class_weight='balanced')`` wrapped in
``CalibratedClassifierCV``, and writes the artifact to the chosen output path.

Split strategies:

* ``daily-stratified`` (default): within each calendar day, the first 80 % of
  rows go to train and the rest to test. Honors time ordering while keeping
  both classes represented on each side — the chronological split used in v1
  is pathological on this corpus because confirmed claims cluster in the
  most recent days while archived claims stop accruing once steward finishes
  its archive sweep for the period, producing a test set of ~94 % positives
  the model has never seen labelled that way during training. See
  ``artifacts/steward-classifier-feature-audit-2026-04-23.md``.
* ``chronological``: legacy behaviour. Kept for backwards comparison and for
  v3 acceptance-test reporting.

``--version`` selects which classifier family to emit:

* ``v2``: same as before — calibration method ``isotonic``, output
  ``artifacts/steward-classifier-v2.joblib``.
* ``v3`` (default): calibration method ``sigmoid`` (the v3 spec calls for
  ``CalibratedClassifierCV(LogisticRegression, method='sigmoid')``), output
  ``artifacts/steward-classifier-v3.joblib``. The training report prints
  ROC-AUC on BOTH the sound (daily-stratified) and chronological splits so
  the apples-to-apples comparison against v2's 0.990 / 0.45 headline numbers
  is immediate.

Prints ROC-AUC on the test split plus precision/recall @ ``--threshold``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import precision_score, recall_score, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memorymaster.steward_features import FEATURE_KEYS, FEATURE_VERSION  # noqa: E402


def _parse_iso(ts: str | None) -> datetime:
    if not ts:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return datetime.fromtimestamp(0, tz=timezone.utc)


def load_rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def to_matrix(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    X = np.asarray([[float(r["features"][k]) for k in FEATURE_KEYS] for r in rows],
                   dtype=np.float64)
    y = np.asarray([int(r["label"]) for r in rows], dtype=np.int64)
    return X, y


def chronological_split(rows: list[dict], train_frac: float = 0.8) -> tuple[list[dict], list[dict]]:
    rows_sorted = sorted(rows, key=lambda r: _parse_iso(r.get("created_at")))
    split = int(len(rows_sorted) * train_frac)
    return rows_sorted[:split], rows_sorted[split:]


def daily_stratified_split(
    rows: list[dict], train_frac: float = 0.8
) -> tuple[list[dict], list[dict]]:
    """Time-aware, class-preserving split: bucket rows by calendar day and
    take the first ``train_frac`` within each bucket for training.

    This keeps positives and negatives proportionally represented on each
    side of the split even when archives and confirmations are produced in
    distinct day clusters. Equivalent to the chronological split when the
    corpus is one day wide."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        day = (r.get("created_at") or "")[:10] or "0000-00-00"
        by_day[day].append(r)
    train: list[dict] = []
    test: list[dict] = []
    for day in sorted(by_day.keys()):
        items = sorted(by_day[day], key=lambda r: _parse_iso(r.get("created_at")))
        cut = int(len(items) * train_frac)
        train.extend(items[:cut])
        test.extend(items[cut:])
    return train, test


def split_rows(rows: list[dict], strategy: str, train_frac: float) -> tuple[list[dict], list[dict]]:
    if strategy == "chronological":
        return chronological_split(rows, train_frac=train_frac)
    if strategy == "daily-stratified":
        return daily_stratified_split(rows, train_frac=train_frac)
    raise ValueError(f"unknown split strategy: {strategy!r}")


def train(train_rows: list[dict], test_rows: list[dict], *,
          threshold: float = 0.65,
          calibration_method: str = "isotonic",
          random_state: int = 42) -> tuple[CalibratedClassifierCV, dict]:
    X_train, y_train = to_matrix(train_rows)
    X_test, y_test = to_matrix(test_rows)
    # Scale inside the pipeline so the calibrated wrapper re-fits it on each
    # fold consistently. Regularization strength C=0.5 mildly discourages
    # overfitting the time-proxy session_age_days.
    base = Pipeline([
        ("scaler", StandardScaler()),
        ("lr", LogisticRegression(class_weight="balanced", max_iter=5000,
                                  C=0.5, solver="lbfgs",
                                  random_state=random_state)),
    ])
    model = CalibratedClassifierCV(base, method=calibration_method, cv=3)
    model.fit(X_train, y_train)

    probs = model.predict_proba(X_test)[:, 1]
    preds = (probs >= threshold).astype(int)
    metrics = {
        "threshold": threshold,
        "n_train": len(train_rows),
        "n_test": len(test_rows),
        "roc_auc": float(roc_auc_score(y_test, probs)) if len(set(y_test)) > 1 else float("nan"),
        "precision": float(precision_score(y_test, preds, zero_division=0)),
        "recall": float(recall_score(y_test, preds, zero_division=0)),
        "positives_test": int(y_test.sum()),
        "negatives_test": int((1 - y_test).sum()),
        "calibration": calibration_method,
    }
    return model, metrics


def eval_only(model: CalibratedClassifierCV, test_rows: list[dict], *,
              threshold: float = 0.65) -> dict:
    """Score an already-trained model against a fresh test partition (no refit)."""
    X_test, y_test = to_matrix(test_rows)
    probs = model.predict_proba(X_test)[:, 1]
    preds = (probs >= threshold).astype(int)
    return {
        "threshold": threshold,
        "n_test": len(test_rows),
        "roc_auc": float(roc_auc_score(y_test, probs)) if len(set(y_test)) > 1 else float("nan"),
        "precision": float(precision_score(y_test, preds, zero_division=0)),
        "recall": float(recall_score(y_test, preds, zero_division=0)),
        "positives_test": int(y_test.sum()),
        "negatives_test": int((1 - y_test).sum()),
    }


def save_artifact(model: CalibratedClassifierCV, out_path: Path, metrics: dict,
                  *, chrono_roc_auc: float | None = None) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "model": model,
        "feature_version": FEATURE_VERSION,
        "feature_keys": list(FEATURE_KEYS),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_train": metrics["n_train"],
        "n_test": metrics["n_test"],
        "roc_auc": metrics["roc_auc"],
        "calibration": metrics.get("calibration", "isotonic"),
    }
    if chrono_roc_auc is not None:
        payload["roc_auc_chronological"] = chrono_roc_auc
    joblib.dump(payload, out_path)


_DEFAULT_OUT_FOR_VERSION = {
    "v2": Path("artifacts/steward-classifier-v2.joblib"),
    "v3": Path("artifacts/steward-classifier-v3.joblib"),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", type=Path,
                    default=Path("tests/fixtures/steward_training.jsonl"))
    ap.add_argument("--out", type=Path, default=None,
                    help="Artifact output path. Defaults to "
                         "artifacts/steward-classifier-<version>.joblib.")
    ap.add_argument("--version", choices=("v2", "v3"), default="v3",
                    help="Classifier family to train. v2 uses isotonic "
                         "calibration (legacy default); v3 uses sigmoid "
                         "calibration per the v3 spec and reports ROC-AUC "
                         "on both the daily-stratified and chronological "
                         "splits so the v3 vs v2 acceptance test is "
                         "apples-to-apples.")
    ap.add_argument("--threshold", type=float, default=0.65)
    ap.add_argument("--train-frac", type=float, default=0.8)
    ap.add_argument("--split", choices=("daily-stratified", "chronological"),
                    default="daily-stratified",
                    help="Split strategy for the PRIMARY training artifact. "
                         "daily-stratified preserves class balance within "
                         "each calendar day (default). chronological "
                         "reproduces the v1 behaviour.")
    ap.add_argument("--random-state", type=int, default=42,
                    help="Seed for the LogisticRegression to keep training "
                         "deterministic across runs.")
    args = ap.parse_args()

    rows = load_rows(args.in_path)
    if not rows:
        print(f"[error] {args.in_path} is empty; run build_steward_training_set.py first")
        return 1
    train_rows, test_rows = split_rows(rows, args.split, args.train_frac)
    if not train_rows or not test_rows:
        print(f"[error] split produced empty partitions (n={len(rows)})")
        return 1

    calibration = "sigmoid" if args.version == "v3" else "isotonic"
    out_path = args.out or _DEFAULT_OUT_FOR_VERSION[args.version]

    model, metrics = train(
        train_rows, test_rows,
        threshold=args.threshold,
        calibration_method=calibration,
        random_state=args.random_state,
    )

    # For v3 we MUST also report the chronological split (per the acceptance
    # spec). We retrain on the chronological partition to get a number that
    # matches what `--split chronological` would produce end-to-end — the
    # spec's v2 0.45 baseline was produced this way.
    chrono_metrics: dict | None = None
    if args.version == "v3":
        c_train, c_test = chronological_split(rows, train_frac=args.train_frac)
        if c_train and c_test:
            _chrono_model, chrono_metrics = train(
                c_train, c_test,
                threshold=args.threshold,
                calibration_method=calibration,
                random_state=args.random_state,
            )

    save_artifact(
        model, out_path, metrics,
        chrono_roc_auc=(chrono_metrics or {}).get("roc_auc"),
    )

    print(f"[train] version={args.version} calibration={calibration}")
    print(f"[train] rows={len(rows)} primary_split={args.split} "
          f"train={metrics['n_train']} test={metrics['n_test']}")
    print(f"[train] test class counts: pos={metrics['positives_test']} "
          f"neg={metrics['negatives_test']}")
    print(f"[train] ROC-AUC ({args.split})={metrics['roc_auc']:.4f}")
    if chrono_metrics is not None:
        print(f"[train] ROC-AUC (chronological)={chrono_metrics['roc_auc']:.4f} "
              f"(train={chrono_metrics['n_train']} test={chrono_metrics['n_test']})")
        print("[train] v2 baselines for comparison: sound=0.9898  chronological=0.45 "
              "(v3 target: strictly > 0.45; stretch >= 0.60)")
    print(f"[train] @threshold={metrics['threshold']}: "
          f"precision={metrics['precision']:.4f} recall={metrics['recall']:.4f}")
    print(f"[train] artifact -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
