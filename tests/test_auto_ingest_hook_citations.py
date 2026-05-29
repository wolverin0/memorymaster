"""Regression tests for the auto-ingest stop hook citations invariant.

History:
- #128 (2026-04-22 audit): the hook inserted claims via raw SQL but never
  inserted the companion ``citations`` row, so every hook-born claim failed the
  steward ``min_citations >= 1`` gate and stayed unpromotable forever.
- v3.24 (F3 refactor): the hook's ``_run_gemini_extraction`` now routes through
  ``MemoryService.ingest`` with an explicit ``CitationInput`` instead of raw
  SQL, gaining the canonical ingest path (sensitivity sanitize, dedup, entity
  resolution, observability) AND keeping the citation guarantee.

The invariant under test is unchanged — **every hook-born claim must carry at
least one citation** — but it is now enforced by the service path, not a raw
citations INSERT. These tests verify that intent against the real service and
guard the template's use of the service + CitationInput.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from memorymaster.models import CitationInput
from memorymaster.service import MemoryService

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "memorymaster" / "config_templates" / "hooks" / "memorymaster-auto-ingest.py"


@pytest.fixture
def svc(tmp_path):
    s = MemoryService(tmp_path / "hook.db", workspace_root=tmp_path)
    s.init_db()
    return s


def _hook_ingest(svc: MemoryService, claim: dict, scope: str):
    """Mirror of the fixed auto-ingest hook's per-claim ingest call.

    Matches _run_gemini_extraction in the template exactly: route through
    service.ingest with a CitationInput sourced to the hook, so the claim is
    born with a citation (promotable) and through the canonical filter path.
    """
    text = claim["text"]
    text_hash = hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]
    return svc.ingest(
        text=text,
        citations=[CitationInput(source="llm-stop-hook", locator=scope, excerpt=text[:200])],
        idempotency_key=f"llm-stop-{text_hash}",
        claim_type=claim.get("claim_type", "fact"),
        subject=claim.get("subject", "codebase"),
        predicate=claim.get("predicate", "observation"),
        scope=scope,
        confidence=0.6,
        source_agent="llm-stop-hook",
    )


def test_hook_path_creates_citation_per_claim(svc):
    claims = [
        {"text": "Recall hook needs skip_qdrant=True on Windows environments"},
        {"text": "Sensitivity filter F1 0.995 on adversarial corpus example"},
        {"text": "Scope canonicalization folds Copy/dash/underscore variants"},
    ]
    ids = [_hook_ingest(svc, c, "project:memorymaster").id for c in claims]

    for cid in ids:
        fetched = svc.store.get_claim(cid, include_citations=True)
        assert fetched is not None
        assert fetched.citations, f"hook-born claim {cid} has no citation (#128 regression)"


def test_hook_citation_locator_is_scope(svc):
    """The citation's source is the hook and its locator carries the scope so
    audits can trace origin."""
    claim = _hook_ingest(svc, {"text": "a hook-born claim about something"}, "project:memorymaster")
    fetched = svc.store.get_claim(claim.id, include_citations=True)
    cite = fetched.citations[0]
    assert cite.source == "llm-stop-hook"
    assert cite.locator == "project:memorymaster"


def test_template_routes_claims_through_service_with_citation():
    """Source guard: the template must ingest hook claims via MemoryService.ingest
    with a CitationInput (the v3.24 mechanism that guarantees the citation),
    NOT raw SQL. Tripwire against a refactor that drops the citation guarantee."""
    assert TEMPLATE_PATH.exists(), f"Template missing at {TEMPLATE_PATH}"
    src = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "MemoryService" in src and "svc.ingest(" in src, (
        "Template no longer ingests via MemoryService.ingest — did the hook path move?"
    )
    assert "CitationInput(source=\"llm-stop-hook\"" in src, (
        "Template ingests claims without a hook CitationInput. This regresses #128: "
        "every hook-born claim becomes unpromotable (fails steward min_citations gate)."
    )
    # The old raw-SQL path must be gone (it bypassed the service/filter).
    assert "INSERT INTO claims" not in src, (
        "Template still contains a raw INSERT INTO claims — the v3.24 F3 refactor "
        "routed gemini extraction through the service; raw SQL should be removed."
    )
