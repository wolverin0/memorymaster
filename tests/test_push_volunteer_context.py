"""Intent-anchored tests for the volunteer_context (push/volunteer) MCP tool.

WHY this feature exists: a pre-prompt hook wants to *proactively* surface
relevant claims from recent turns, but only the high-confidence ones — flooding
the context window with weak guesses is worse than silence. ``volunteer_context``
clones ``query_for_context`` and adds a ``min_confidence`` post-filter gate.

The invariants these tests anchor on (the *requirement*, not the mechanics):

1. The gate actually gates — nothing below min_confidence survives.
2. The gate is purely ADDITIVE — open (0.0) it must reproduce query_for_context
   byte-for-byte, so existing recall behaviour is unchanged by default.
3. An empty high-confidence result is a graceful ok=True / 0-claims path, not
   an error — a hook must be able to call this every turn and get silence.
4. Sensitive claims are NEVER volunteered (the firewall holds on this new path).
5. Calling volunteer_context has no side-effect on the retrieval stack — a
   later query_for_context with the same query is unaffected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


QUERY_TOKEN = "volunteercontexttoken"


def _tools():
    try:
        from memorymaster.surfaces.mcp_server import (
            init_db,
            ingest_claim,
            query_for_context,
            volunteer_context,
        )
    except ImportError:  # pragma: no cover - MCP not installed
        pytest.skip("MCP not installed")
    return init_db, ingest_claim, query_for_context, volunteer_context


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    db_path = str(tmp_path / "test.db")
    workspace = str(tmp_path)
    init_db, ingest_claim, query_for_context, volunteer_context = _tools()
    init_db(db=db_path, workspace=workspace)
    return {
        "db": db_path,
        "workspace": workspace,
        "ingest_claim": ingest_claim,
        "query_for_context": query_for_context,
        "volunteer_context": volunteer_context,
    }


def _ingest(env, text: str, confidence: float):
    return env["ingest_claim"](
        text=text,
        db=env["db"],
        workspace=env["workspace"],
        sources_json='["test.py"]',
        confidence=confidence,
    )


def test_gate_excludes_low_confidence_claims(env):
    """WHY: a hook asking for min_confidence=0.8 must not receive a 0.3 claim —
    otherwise the "high-confidence only" promise is broken and the window fills
    with weak guesses. Anchored on the confidence of every returned claim, not
    on a count, so it stays true regardless of ranking internals.
    """
    _ingest(env, f"{QUERY_TOKEN} strong claim", confidence=0.95)
    _ingest(env, f"{QUERY_TOKEN} weak claim", confidence=0.30)

    result = env["volunteer_context"](
        query=QUERY_TOKEN,
        db=env["db"],
        workspace=env["workspace"],
        min_confidence=0.8,
        detail_level="summary",
        trust_mode="exploratory",
    )
    assert result["ok"] is True
    confidences = [c["confidence"] for c in result.get("claims", [])]
    assert confidences, "expected at least the high-confidence claim to survive"
    assert all(c >= 0.8 for c in confidences), confidences


def test_open_gate_matches_query_for_context(env):
    """WHY: the gate must be ADDITIVE. With min_confidence=0.0 every ranked row
    passes, so the packed output and counts must be identical to
    query_for_context with the same args. This proves volunteer_context does not
    alter default recall — only filters when explicitly asked to.
    """
    _ingest(env, f"{QUERY_TOKEN} alpha claim", confidence=0.9)
    _ingest(env, f"{QUERY_TOKEN} beta claim", confidence=0.4)

    common = dict(
        query=QUERY_TOKEN,
        db=env["db"],
        workspace=env["workspace"],
        token_budget=2000,
        trust_mode="exploratory",
    )
    volunteered = env["volunteer_context"](min_confidence=0.0, **common)
    baseline = env["query_for_context"](**common)

    assert volunteered["output"] == baseline["output"]
    assert volunteered["claims_included"] == baseline["claims_included"]
    assert volunteered["claims_considered"] == baseline["claims_considered"]
    assert volunteered["tokens_used"] == baseline["tokens_used"]


def test_no_high_confidence_match_returns_empty_ok(env):
    """WHY: a hook fires every turn; when nothing clears the bar it must get a
    graceful empty result (ok=True, 0 claims, empty output), never an error or a
    crash. Silence is a valid, common answer.
    """
    _ingest(env, f"{QUERY_TOKEN} only weak claim", confidence=0.2)

    result = env["volunteer_context"](
        query=QUERY_TOKEN,
        db=env["db"],
        workspace=env["workspace"],
        min_confidence=0.9,
        trust_mode="exploratory",
    )
    assert result["ok"] is True
    # The graceful-empty contract: zero claims volunteered (the formatted block
    # is just a "no claims fit" placeholder, never an error). A hook can call
    # this every turn and safely inject nothing.
    assert result["claims_included"] == 0
    assert result["claims_considered"] == 0
    assert "only weak claim" not in result["output"]


def test_sensitive_claim_never_volunteered(tmp_path, monkeypatch):
    """WHY: volunteer_context is a NEW read path. The sensitivity firewall must
    hold here too — a claim that is_sensitive_claim flags (e.g. one carrying a
    redacted secret marker) must never be surfaced, even with the gate fully
    open and its confidence at the top. We seed a claim whose text trips the
    secret scanner (the service redacts it to a [REDACTED:] marker, which
    is_sensitive_claim then flags) and assert it is absent while the benign
    claim is present.
    """
    from memorymaster.core.security import is_sensitive_claim

    monkeypatch.delenv("QDRANT_URL", raising=False)
    init_db, ingest_claim, _qfc, volunteer_context = _tools()
    db_path = str(tmp_path / "sens.db")
    workspace = str(tmp_path)

    svc = MemoryService(db_target=db_path, workspace_root=Path(workspace))
    svc.init_db()
    public_text = f"{QUERY_TOKEN} public visible note about deployment"
    # AKIA... is a real AWS-access-key shape: the ingest filter redacts it, and
    # the stored claim then reads as sensitive at query time.
    secret_seed = f"{QUERY_TOKEN} AWS key AKIAIOSFODNN7EXAMPLE leaked"

    def cite(t):
        return [CitationInput(source="synthetic-test", locator="t", excerpt=t)]

    svc.ingest(text=public_text, citations=cite(public_text), scope="project",
               claim_type="fact", source_agent="agentA", visibility="public", confidence=0.95)
    sensitive_claim = svc.ingest(text=secret_seed, citations=cite(secret_seed), scope="project",
                                 claim_type="fact", source_agent="agentA", visibility="public", confidence=0.99)
    # Guard the test's own premise: the seed must actually be flagged sensitive,
    # otherwise this test would silently stop testing anything.
    assert is_sensitive_claim(sensitive_claim), "seed claim was not flagged sensitive — test premise broken"

    result = volunteer_context(
        query=QUERY_TOKEN,
        db=db_path,
        workspace=workspace,
        min_confidence=0.0,
        detail_level="standard",
        trust_mode="exploratory",
    )
    assert result["ok"] is True
    assert "[REDACTED:" not in (result["output"] or ""), "redacted-secret claim leaked into volunteered output"
    assert result["claims_included"] == 1, "exactly the one benign claim should be volunteered"
    assert public_text in (result["output"] or ""), "benign claim missing from volunteered output"


def test_no_side_effect_on_subsequent_query(env):
    """WHY: a push tool must be observation-only on the retrieval stack. Calling
    volunteer_context (even with an aggressive gate) must not mutate state that
    changes a later query_for_context for the same query — no cache poisoning,
    no consumed rows. Anchors the 'pure read' contract.
    """
    _ingest(env, f"{QUERY_TOKEN} durable claim", confidence=0.5)

    common = dict(
        query=QUERY_TOKEN,
        db=env["db"],
        workspace=env["workspace"],
        trust_mode="exploratory",
    )
    before = env["query_for_context"](**common)
    env["volunteer_context"](min_confidence=0.99, **common)  # gate out everything
    after = env["query_for_context"](**common)

    assert before["output"] == after["output"]
    assert before["claims_included"] == after["claims_included"]
