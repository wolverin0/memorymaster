"""Centralized configuration for tunable MemoryMaster constants.

All values have sensible defaults matching prior hardcoded behavior.
Override via environment variables or by calling ``load_config()`` with
a JSON config file path.

Environment variables
---------------------
MEMORYMASTER_RETRIEVAL_WEIGHTS
    Comma-separated floats for hybrid ranking: lexical,confidence,freshness,vector.
    Default: ``0.45,0.30,0.15,0.10``

MEMORYMASTER_RETRIEVAL_WEIGHTS_NO_VECTOR
    Weights when vector search is disabled: lexical,confidence,freshness.
    Default: ``0.55,0.30,0.15``

MEMORYMASTER_LEXICAL_WEIGHTS
    Weights for lexical sub-score: recall,precision,phrase,prefix.
    Default: ``0.55,0.15,0.25,0.05``

MEMORYMASTER_FRESHNESS_HALFLIFE
    Freshness half-life hours by volatility: low,medium,high.
    Default: ``168.0,72.0,24.0``

MEMORYMASTER_CADENCE_HOURS
    Base revalidation cadence hours by volatility: low,medium,high.
    Default: ``168.0,72.0,24.0``

MEMORYMASTER_DECAY_RATES
    Daily decay rates by volatility: low,medium,high.
    Default: ``0.01,0.03,0.06``

MEMORYMASTER_VALIDATION_THRESHOLD
    Minimum score for a claim to pass validation.
    Default: ``0.58``

MEMORYMASTER_STALE_THRESHOLD
    Confidence below which a claim transitions to stale via decay.
    Default: ``0.35``

MEMORYMASTER_CONFLICT_MARGIN
    Score margin for conflict detection against existing claims.
    Default: ``0.08``

MEMORYMASTER_PINNED_BONUS
    Score bonus applied to pinned claims during ranking.
    Default: ``0.03``

MEMORYMASTER_CONFIG_FILE
    Path to a JSON config file. Keys match attribute names on ``Config``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _parse_floats(raw: str, expected: int) -> list[float]:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) != expected:
        raise ValueError(f"Expected {expected} comma-separated floats, got {len(parts)}: {raw!r}")
    return [float(p) for p in parts]


def _parse_volatility_dict(raw: str) -> Dict[str, float]:
    values = _parse_floats(raw, 3)
    return {"low": values[0], "medium": values[1], "high": values[2]}


@dataclass(frozen=True)
class Config:
    """Immutable configuration for tunable MemoryMaster constants."""

    # --- Retrieval ranking weights (hybrid mode with vector) ---
    retrieval_weight_lexical: float = 0.45
    retrieval_weight_confidence: float = 0.30
    retrieval_weight_freshness: float = 0.15
    retrieval_weight_vector: float = 0.10

    # --- Retrieval ranking weights (hybrid mode without vector) ---
    retrieval_weight_lexical_no_vector: float = 0.55
    retrieval_weight_confidence_no_vector: float = 0.30
    retrieval_weight_freshness_no_vector: float = 0.15

    # --- Lexical sub-score weights ---
    lexical_weight_recall: float = 0.55
    lexical_weight_precision: float = 0.15
    lexical_weight_phrase: float = 0.25
    lexical_weight_prefix: float = 0.05

    # --- Freshness half-life by volatility (hours) ---
    freshness_half_life_low: float = 168.0
    freshness_half_life_medium: float = 72.0
    freshness_half_life_high: float = 24.0

    # --- Revalidation cadence by volatility (hours) ---
    cadence_hours_low: float = 168.0
    cadence_hours_medium: float = 72.0
    cadence_hours_high: float = 24.0

    # --- Decay rates by volatility (daily) ---
    decay_rate_low: float = 0.005
    decay_rate_medium: float = 0.02
    decay_rate_high: float = 0.05

    # --- Thresholds ---
    validation_threshold: float = 0.58
    stale_threshold: float = 0.35
    conflict_margin: float = 0.08
    pinned_bonus: float = 0.03

    # --- Derived convenience dicts ---

    @property
    def freshness_half_life_hours(self) -> Dict[str, float]:
        return {
            "low": self.freshness_half_life_low,
            "medium": self.freshness_half_life_medium,
            "high": self.freshness_half_life_high,
        }

    @property
    def cadence_hours(self) -> Dict[str, float]:
        return {
            "low": self.cadence_hours_low,
            "medium": self.cadence_hours_medium,
            "high": self.cadence_hours_high,
        }

    @property
    def decay_rates(self) -> Dict[str, float]:
        return {
            "low": self.decay_rate_low,
            "medium": self.decay_rate_medium,
            "high": self.decay_rate_high,
        }

    @property
    def retrieval_weights(self) -> tuple[float, float, float, float]:
        return (
            self.retrieval_weight_lexical,
            self.retrieval_weight_confidence,
            self.retrieval_weight_freshness,
            self.retrieval_weight_vector,
        )

    @property
    def retrieval_weights_no_vector(self) -> tuple[float, float, float]:
        return (
            self.retrieval_weight_lexical_no_vector,
            self.retrieval_weight_confidence_no_vector,
            self.retrieval_weight_freshness_no_vector,
        )

    @property
    def lexical_weights(self) -> tuple[float, float, float, float]:
        return (
            self.lexical_weight_recall,
            self.lexical_weight_precision,
            self.lexical_weight_phrase,
            self.lexical_weight_prefix,
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_config: Config | None = None


def get_config() -> Config:
    """Return the current configuration, loading defaults if needed."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def set_config(cfg: Config) -> None:
    """Replace the global configuration (useful for testing)."""
    global _config
    _config = cfg


def reset_config() -> None:
    """Reset to None so next ``get_config()`` re-reads env vars."""
    global _config
    _config = None


def load_config(config_path: str | Path | None = None) -> Config:
    """Build a ``Config`` from env vars, optionally overlaying a JSON file.

    The JSON file may be specified via *config_path* or via the
    ``MEMORYMASTER_CONFIG_FILE`` environment variable. Keys in the JSON
    correspond to ``Config`` attribute names.
    """
    overrides: dict[str, object] = {}

    # --- Load JSON file if provided ---
    path = config_path or os.environ.get("MEMORYMASTER_CONFIG_FILE", "").strip()
    if path:
        p = Path(path)
        if p.is_file():
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                overrides.update(data)

    # --- Environment variable overrides ---
    _apply_env_floats(overrides, "MEMORYMASTER_RETRIEVAL_WEIGHTS", 4, [
        "retrieval_weight_lexical",
        "retrieval_weight_confidence",
        "retrieval_weight_freshness",
        "retrieval_weight_vector",
    ])
    _apply_env_floats(overrides, "MEMORYMASTER_RETRIEVAL_WEIGHTS_NO_VECTOR", 3, [
        "retrieval_weight_lexical_no_vector",
        "retrieval_weight_confidence_no_vector",
        "retrieval_weight_freshness_no_vector",
    ])
    _apply_env_floats(overrides, "MEMORYMASTER_LEXICAL_WEIGHTS", 4, [
        "lexical_weight_recall",
        "lexical_weight_precision",
        "lexical_weight_phrase",
        "lexical_weight_prefix",
    ])
    _apply_env_floats(overrides, "MEMORYMASTER_FRESHNESS_HALFLIFE", 3, [
        "freshness_half_life_low",
        "freshness_half_life_medium",
        "freshness_half_life_high",
    ])
    _apply_env_floats(overrides, "MEMORYMASTER_CADENCE_HOURS", 3, [
        "cadence_hours_low",
        "cadence_hours_medium",
        "cadence_hours_high",
    ])
    _apply_env_floats(overrides, "MEMORYMASTER_DECAY_RATES", 3, [
        "decay_rate_low",
        "decay_rate_medium",
        "decay_rate_high",
    ])

    _apply_env_float(overrides, "MEMORYMASTER_VALIDATION_THRESHOLD", "validation_threshold")
    _apply_env_float(overrides, "MEMORYMASTER_STALE_THRESHOLD", "stale_threshold")
    _apply_env_float(overrides, "MEMORYMASTER_CONFLICT_MARGIN", "conflict_margin")
    _apply_env_float(overrides, "MEMORYMASTER_PINNED_BONUS", "pinned_bonus")

    # Filter to only valid Config fields
    valid_fields = {f.name for f in Config.__dataclass_fields__.values()}
    filtered = {k: v for k, v in overrides.items() if k in valid_fields}

    return Config(**filtered)


def _apply_env_floats(
    overrides: dict[str, object],
    env_var: str,
    count: int,
    keys: list[str],
) -> None:
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return
    values = _parse_floats(raw, count)
    for key, val in zip(keys, values):
        overrides[key] = val


def _apply_env_float(overrides: dict[str, object], env_var: str, key: str) -> None:
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return
    overrides[key] = float(raw)
