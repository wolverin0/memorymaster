"""Environment configuration for Jev decisions.

Every value has a safe default; an invalid value never enables anything.  Truthiness
is parsed identically for every flag in this package: ``1/true/yes/on`` (any case,
surrounding whitespace ignored) is true, everything else is false.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

SURFACES: tuple[str, ...] = ("revalidate", "recall", "ingest", "dedup", "skills", "hints", "route", "session")
MODES: tuple[str, ...] = ("off", "shadow", "live")
TRUTHY = frozenset({"1", "true", "yes", "on"})

MODE_ENV = "MEMORYMASTER_JEV_MODE"
DAILY_USD_CAP_ENV = "MEMORYMASTER_JEV_DAILY_USD_CAP"
RPM_CAP_ENV = "MEMORYMASTER_JEV_RPM_CAP"
HOOK_DEADLINE_ENV = "MEMORYMASTER_JEV_HOOK_DEADLINE_MS"
BATCH_DEADLINE_ENV = "MEMORYMASTER_JEV_BATCH_DEADLINE_MS"
DECISIONS_DB_ENV = "MEMORYMASTER_DECISIONS_DB"
RETENTION_ENV = "MEMORYMASTER_DECISIONS_STATE_RETENTION_DAYS"
LOG_OFF_ENV = "MEMORYMASTER_DECISIONS_LOG_OFF"
HOOK_BUSY_ENV = "MEMORYMASTER_DECISIONS_HOOK_BUSY_MS"
BREAKER_PROBE_ENV = "MEMORYMASTER_JEV_BREAKER_PROBE_S"

DEFAULT_DAILY_USD_CAP = 2.0
DEFAULT_RPM_CAP = 600
DEFAULT_HOOK_DEADLINE_MS = 900
DEFAULT_BATCH_DEADLINE_MS = 8000
DEFAULT_RETENTION_DAYS = 180
# A hook waits at most this long for another writer of decisions.db (per connection).
DEFAULT_HOOK_BUSY_MS = 250
# While a surface's breaker is open, one shadow probe at most this often (seconds).
DEFAULT_BREAKER_PROBE_S = 60
DEFAULT_EXPLORE_RATES: Mapping[str, float] = {"recall": 0.10, "ingest": 0.05}


def env_truthy(value: object) -> bool:
    """Shared truthiness: only ``1/true/yes/on`` (case-insensitive) are true."""
    return isinstance(value, str) and value.strip().lower() in TRUTHY


def surface_env(surface: str) -> str:
    return f"MEMORYMASTER_JEV_{surface.strip().upper()}"


def explore_env(surface: str) -> str:
    return f"MEMORYMASTER_JEV_EXPLORE_{surface.strip().upper()}"


def default_decisions_db() -> Path:
    return Path.home() / ".memorymaster" / "decisions.db"


def _mode(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    value = raw.strip().lower()
    return value if value in MODES else "off"


def _positive_float(raw: str | None, default: float) -> float:
    try:
        value = float(raw) if raw is not None and raw.strip() else default
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


def _positive_int(raw: str | None, default: int) -> int:
    try:
        value = int(raw) if raw is not None and raw.strip() else default
    except ValueError:
        return default
    return value if value > 0 else default


def _rate(raw: str | None, default: float) -> float:
    try:
        value = float(raw) if raw is not None and raw.strip() else default
    except ValueError:
        return default
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else default


@dataclass(frozen=True)
class DecisionConfig:
    """Immutable snapshot of the decision environment."""

    global_mode: str = "off"
    surface_modes: Mapping[str, str] = field(default_factory=dict)
    daily_usd_cap: float = DEFAULT_DAILY_USD_CAP
    rpm_cap: int = DEFAULT_RPM_CAP
    hook_deadline_ms: int = DEFAULT_HOOK_DEADLINE_MS
    batch_deadline_ms: int = DEFAULT_BATCH_DEADLINE_MS
    explore_rates: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_EXPLORE_RATES))
    decisions_db: Path = field(default_factory=default_decisions_db)
    state_retention_days: int = DEFAULT_RETENTION_DAYS
    log_off: bool = False
    hook_busy_ms: int = DEFAULT_HOOK_BUSY_MS
    breaker_probe_s: int = DEFAULT_BREAKER_PROBE_S

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "DecisionConfig":
        source = os.environ if environ is None else environ
        global_mode = _mode(source.get(MODE_ENV)) or "off"
        surface_modes: dict[str, str] = {}
        explore_rates: dict[str, float] = {}
        for surface in SURFACES:
            override = _mode(source.get(surface_env(surface)))
            if override is not None:
                surface_modes[surface] = override
            explore_rates[surface] = _rate(source.get(explore_env(surface)), DEFAULT_EXPLORE_RATES.get(surface, 0.0))
        db_raw = (source.get(DECISIONS_DB_ENV) or "").strip()
        return cls(
            global_mode=global_mode,
            surface_modes=surface_modes,
            daily_usd_cap=_positive_float(source.get(DAILY_USD_CAP_ENV), DEFAULT_DAILY_USD_CAP),
            rpm_cap=_positive_int(source.get(RPM_CAP_ENV), DEFAULT_RPM_CAP),
            hook_deadline_ms=_positive_int(source.get(HOOK_DEADLINE_ENV), DEFAULT_HOOK_DEADLINE_MS),
            batch_deadline_ms=_positive_int(source.get(BATCH_DEADLINE_ENV), DEFAULT_BATCH_DEADLINE_MS),
            explore_rates=explore_rates,
            decisions_db=Path(db_raw).expanduser() if db_raw else default_decisions_db(),
            state_retention_days=_positive_int(source.get(RETENTION_ENV), DEFAULT_RETENTION_DAYS),
            log_off=env_truthy(source.get(LOG_OFF_ENV)),
            hook_busy_ms=_positive_int(source.get(HOOK_BUSY_ENV), DEFAULT_HOOK_BUSY_MS),
            breaker_probe_s=_positive_int(source.get(BREAKER_PROBE_ENV), DEFAULT_BREAKER_PROBE_S),
        )

    def mode_for(self, surface: str) -> str:
        key = surface.strip().lower()
        if key not in SURFACES:
            return "off"
        return self.surface_modes.get(key, self.global_mode)

    def explore_rate(self, surface: str) -> float:
        key = surface.strip().lower()
        if key not in SURFACES:
            return 0.0
        return float(self.explore_rates.get(key, DEFAULT_EXPLORE_RATES.get(key, 0.0)))

    def deadline_ms(self, kind: str) -> int:
        return self.batch_deadline_ms if kind == "batch" else self.hook_deadline_ms


__all__ = [
    "DecisionConfig",
    "MODES",
    "SURFACES",
    "env_truthy",
    "explore_env",
    "surface_env",
]
