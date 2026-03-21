"""RL model trainer — trains a write-quality predictor from feedback data.

When enough feedback rows exist (100+), trains a gradient boosted tree
to predict which claims will be useful. Falls back to heuristic scoring
when sklearn is not available or insufficient data.

Usage:
    memorymaster train-model
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from memorymaster.feedback import FeedbackTracker

logger = logging.getLogger(__name__)

MIN_SAMPLES = 100


def train_quality_model(db_path: str) -> dict:
    """Train or retrain the quality prediction model.

    Returns training metrics or skip reason.
    """
    ft = FeedbackTracker(db_path)
    ft.ensure_tables()

    # First compute quality scores from current data
    ft.compute_quality_scores()
    stats = ft.get_stats()

    if stats["feedback_rows"] < MIN_SAMPLES:
        return {
            "status": "skipped",
            "reason": "insufficient_data",
            "feedback_rows": stats["feedback_rows"],
            "min_required": MIN_SAMPLES,
            "suggestion": f"Need {MIN_SAMPLES - stats['feedback_rows']} more queries before training",
        }

    # Try sklearn training
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Build training data from quality_scores + usage_feedback
        rows = conn.execute("""
            SELECT qs.claim_id, qs.quality_score, qs.retrieval_count,
                   c.confidence, c.access_count, c.tier,
                   LENGTH(c.text) as text_length,
                   c.claim_type
            FROM quality_scores qs
            JOIN claims c ON c.id = qs.claim_id
            WHERE c.status != 'archived'
        """).fetchall()
        conn.close()

        if len(rows) < MIN_SAMPLES:
            return {"status": "skipped", "reason": "insufficient_scored_claims", "count": len(rows)}

        # Build feature matrix
        X = []
        y = []
        for row in rows:
            X.append([
                float(row["confidence"]),
                float(row["access_count"]),
                float(row["retrieval_count"]),
                float(row["text_length"]),
                1.0 if row["tier"] == "core" else (0.0 if row["tier"] == "peripheral" else 0.5),
            ])
            y.append(1 if row["quality_score"] > 0.6 else 0)

        try:
            from sklearn.ensemble import GradientBoostingClassifier
            from sklearn.model_selection import cross_val_score
            import joblib
        except ImportError:
            return {
                "status": "skipped",
                "reason": "sklearn_not_installed",
                "suggestion": "pip install scikit-learn joblib",
            }

        model = GradientBoostingClassifier(
            n_estimators=50, max_depth=3, min_samples_leaf=10,
            learning_rate=0.1, random_state=42,
        )

        # Cross-validate
        cv_scores = cross_val_score(model, X, y, cv=min(5, len(y) // 20 or 2), scoring="roc_auc")
        mean_auc = float(cv_scores.mean())

        if mean_auc < 0.55:
            return {
                "status": "skipped",
                "reason": "low_auc",
                "auc": mean_auc,
                "samples": len(rows),
                "suggestion": "Model not better than random — need more diverse feedback",
            }

        # Train on full dataset
        model.fit(X, y)

        # Save
        model_path = str(Path(db_path).parent / "quality_model.pkl")
        if os.path.exists(model_path):
            os.replace(model_path, f"{model_path}.bak")
        joblib.dump(model, model_path)

        feature_names = ["confidence", "access_count", "retrieval_count", "text_length", "tier_score"]
        importances = dict(zip(feature_names, model.feature_importances_))

        return {
            "status": "trained",
            "samples": len(rows),
            "positive_rate": sum(y) / len(y),
            "cv_auc_mean": mean_auc,
            "cv_auc_std": float(cv_scores.std()),
            "top_features": sorted(importances.items(), key=lambda x: -x[1])[:3],
            "model_path": model_path,
        }

    except Exception as exc:
        logger.warning("RL training failed: %s", exc)
        return {"status": "error", "reason": str(exc)}
