"""Bounded, default-off governed-skill review cycle phase."""

from __future__ import annotations

import logging
import os
from typing import Any


_FLAG = "MEMORYMASTER_SKILL_REVIEW"
_LIMIT_ENV = "MEMORYMASTER_SKILL_REVIEW_LIMIT"
_DEFAULT_LIMIT = 5
logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.environ.get(_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def _limit() -> int:
    raw = os.environ.get(_LIMIT_ENV, "").strip()
    if not raw:
        return _DEFAULT_LIMIT
    try:
        return max(1, min(int(raw), 20))
    except ValueError:
        return _DEFAULT_LIMIT


def run(service: Any) -> dict[str, object]:
    """Review recurring rules into candidates without breaking the main cycle."""
    if not _enabled():
        return {"enabled": False}
    db_path = str(getattr(service.store, "db_path", "") or "")
    if not db_path or "://" in db_path:
        return {"enabled": True, "skipped": "no_sqlite_db_path"}
    try:
        from memorymaster.knowledge.skills import review_due_skills

        result = review_due_skills(service, limit=_limit())
        result["enabled"] = True
        return result
    except Exception as exc:
        logger.warning("skill review phase failed: %s", exc)
        return {"enabled": True, "error": str(exc)}
