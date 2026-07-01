"""Takes-vs-Facts epistemology: nullable `holder` field on claims.

Source: gbrain multi-holder-belief dimension (source key: takes_vs_facts).

The belief-TYPE axis (take/fact/bet/hunch) rides on the existing free-form
`claim_type` string; the only net-new surface is `holder: str | None`. These
tests prove the silent-dropper write+read round-trip on SQLite AND that the
default path (no holder + W_HOLDER=0) is ranking-neutral / unchanged.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


def _service(tmp_path: Path) -> MemoryService:
    svc = MemoryService(db_target=str(tmp_path / "x.db"), workspace_root=tmp_path)
    svc.init_db()
    return svc


def test_holder_round_trips_on_sqlite(tmp_path: Path) -> None:
    """WHY: `holder` is the whole feature. If it drops on the write or read
    path, the multi-holder-belief dimension is silently lost — a claim ingested
    as Alice's take comes back holder-agnostic, corrupting attribution."""
    svc = _service(tmp_path)
    claim = svc.ingest(
        text="ORM lazy-loading is a performance footgun in this codebase",
        citations=[CitationInput(source="test://takes")],
        claim_type="take",
        holder="alice",
    )
    reloaded = svc.store.get_claim(claim.id)
    assert reloaded is not None
    assert reloaded.holder == "alice"


def test_holder_defaults_to_none(tmp_path: Path) -> None:
    """WHY: the default (holder-agnostic fact) must be unchanged. An ingest
    without `holder` must store NULL, not an empty string or a placeholder,
    so pre-holder callers stay byte-identical."""
    svc = _service(tmp_path)
    claim = svc.ingest(
        text="Python 3.10 is the minimum supported version",
        citations=[CitationInput(source="test://facts")],
        claim_type="fact",
    )
    reloaded = svc.store.get_claim(claim.id)
    assert reloaded is not None
    assert reloaded.holder is None


def test_w_holder_default_is_zero() -> None:
    """WHY: W_HOLDER=0.0 is the guarantee that the default recall path is
    byte-identical. A non-zero default would silently re-rank every recall the
    moment this ships — the feature must be opt-in, ranking-neutral by default."""
    from memorymaster.recall.context_hook import _RECALL_WEIGHT_DEFAULTS

    assert _RECALL_WEIGHT_DEFAULTS["W_HOLDER"] == 0.0
