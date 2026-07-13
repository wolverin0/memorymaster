"""Red static contract for lifecycle-safe scheduled archival (MM-LIFE-01)."""
from __future__ import annotations

from pathlib import Path

import pytest


HOOK_TEMPLATE = (
    Path(__file__).parents[1]
    / "memorymaster"
    / "config_templates"
    / "hooks"
    / "memorymaster-steward-cycle.py"
)


@pytest.mark.xfail(
    strict=True,
    reason="audit baseline MM-LIFE-01: scheduled hook archives with raw status SQL",
)
def test_scheduled_archive_contains_no_raw_claim_status_update() -> None:
    """Scheduled archival must enter through lifecycle authority and its events."""
    source = HOOK_TEMPLATE.read_text(encoding="utf-8")
    normalized = " ".join(source.lower().split())

    assert "update claims set status = 'archived'" not in normalized
