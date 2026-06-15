"""Runtime wrapper for the calibrated steward promotion classifier (task #129).

Loads the fitted sklearn pipeline from the joblib artifact produced by
``scripts/train_steward_classifier.py``. If the artifact is missing OR its
``feature_version`` differs from the extractor's, ``load_classifier()``
returns ``None`` and callers MUST fall back to the legacy additive formula.
Rollback safety beats recall.

Off-by-default: activates only when ``MEMORYMASTER_STEWARD_CLASSIFIER_PATH``
is set OR ``MEMORYMASTER_STEWARD_CLASSIFIER_ENABLED=1``. Keeps fresh installs
and unit tests on the legacy formula even if an artifact sits at the default
path.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memorymaster.govern.steward_features import (
    FEATURE_KEYS,
    FEATURE_VERSION,
    extract_features,
)

_LOG = logging.getLogger(__name__)

_ENV_VAR = "MEMORYMASTER_STEWARD_CLASSIFIER_PATH"
_ENABLE_ENV = "MEMORYMASTER_STEWARD_CLASSIFIER_ENABLED"
_DEFAULT_PATH = Path("artifacts/steward-classifier-v2.joblib")

_cache_lock = threading.Lock()
_cache: "LoadedClassifier | None" = None
_cache_key: tuple[str, float] | None = None


@dataclass(frozen=True)
class LoadedClassifier:
    model: Any
    feature_version: str
    feature_keys: tuple[str, ...]
    source_path: Path


def _artifact_path() -> Path:
    raw = os.environ.get(_ENV_VAR)
    return Path(raw) if raw else _DEFAULT_PATH


def _is_enabled() -> bool:
    if os.environ.get(_ENV_VAR):
        return True
    return os.environ.get(_ENABLE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _clear_cache() -> None:
    global _cache, _cache_key
    with _cache_lock:
        _cache = None
        _cache_key = None


def load_classifier(path: Path | None = None, *, force_reload: bool = False) -> LoadedClassifier | None:
    """Return the cached classifier, or ``None`` when unavailable/mismatched.
    Explicit ``path=`` always overrides the opt-in env gate (used by tests)."""
    global _cache, _cache_key

    if path is None and not _is_enabled():
        _clear_cache()
        return None

    target = Path(path) if path is not None else _artifact_path()
    try:
        mtime = target.stat().st_mtime
    except FileNotFoundError:
        _clear_cache()
        return None
    except OSError as exc:
        _LOG.warning("classifier stat failed for %s: %s", target, exc)
        return None

    key = (str(target.resolve()), mtime)
    with _cache_lock:
        if not force_reload and _cache is not None and _cache_key == key:
            return _cache

    try:
        import joblib  # lazy so the ml extra stays optional
    except ImportError:
        _LOG.warning("joblib missing — classifier disabled. `pip install memorymaster[ml]`.")
        return None

    try:
        payload = joblib.load(target)
    except Exception as exc:
        _LOG.warning("failed to load classifier %s: %s", target, exc)
        return None

    feature_version = str(payload.get("feature_version", ""))
    if feature_version != FEATURE_VERSION:
        _LOG.warning(
            "classifier feature_version mismatch: artifact=%s extractor=%s — falling back",
            feature_version, FEATURE_VERSION,
        )
        return None

    loaded = LoadedClassifier(
        model=payload["model"],
        feature_version=feature_version,
        feature_keys=tuple(payload.get("feature_keys", FEATURE_KEYS)),
        source_path=target,
    )
    with _cache_lock:
        _cache = loaded
        _cache_key = key
    return loaded


def predict_promote_probability(
    claim: Any,
    conn: sqlite3.Connection,
    *,
    classifier: LoadedClassifier | None = None,
) -> float | None:
    """P(promote) in [0, 1], or ``None`` when the classifier is unavailable.
    Never raises — any failure returns ``None`` so callers can revert to the
    legacy additive formula."""
    clf = classifier or load_classifier()
    if clf is None:
        return None
    try:
        import numpy as np
    except ImportError:
        _LOG.warning("numpy missing — classifier disabled. `pip install memorymaster[ml]`.")
        return None
    try:
        feats = extract_features(claim, conn)
        vec = np.asarray([[float(feats[k]) for k in clf.feature_keys]], dtype=np.float64)
        return float(clf.model.predict_proba(vec)[0][1])
    except Exception as exc:
        _LOG.warning("classifier predict failed: %s — falling back", exc)
        return None


def reset_cache() -> None:
    """Testing helper — drop the cached classifier so a fresh load picks up a new artifact."""
    _clear_cache()
