"""Regression tests for the mcp-server-security audit cluster.

Each test encodes WHY the behaviour matters (the security invariant), not the
implementation detail, so it stays meaningful if the code is refactored.
"""
from __future__ import annotations

import base64

import pytest

import memorymaster.surfaces.mcp_server as mcp_server
from memorymaster.core.security import (
    expand_secret_scan_variants,
    sanitize_claim_input,
    scan_text_for_findings,
)
from memorymaster.core.models import CitationInput


_OPENAI_SECRET = "sk-ABCDEFGHIJKLMNOPQRSTUVWX"  # openai_key-shaped synthetic value


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


# --- Finding 2 (MEDIUM): encoded-secret scan must run at the storage-time
# chokepoint, so EVERY ingest path benefits — not only ingest_claim's text. ---

def test_encoded_secret_in_any_field_is_flagged_sensitive() -> None:
    """WHY: a base64-wrapped credential must never persist in cleartext at rest.

    The literal regex filter cannot see the secret inside its encoded form, so
    without variant expansion the claim would be stored as non-sensitive and
    surfaced by recall. The storage-time sweep must flag it regardless of which
    field carried it (here: object_value, not the scanned-by-MCP text field).
    """
    sanitized = sanitize_claim_input(
        text="harmless description",
        object_value=_b64(_OPENAI_SECRET),
        citations=[CitationInput(source="s", locator="l", excerpt=None)],
    )
    assert sanitized.is_sensitive, "encoded secret in object_value must mark claim sensitive"
    assert "openai_key" in sanitized.findings


def test_shared_decoder_yields_decoded_variant() -> None:
    """WHY: the decoder is the single firewall point; it must actually decode."""
    variants = list(expand_secret_scan_variants(_b64(_OPENAI_SECRET)))
    assert any(_OPENAI_SECRET in v for v in variants)
    assert "openai_key" in scan_text_for_findings(_b64(_OPENAI_SECRET))


# --- Finding 1 (HIGH): ingest_rule must (a) reject sensitive input, (b) honor
# the rate limit, and (c) never echo the RAW rule text back to the client. ---

@pytest.fixture(autouse=True)
def _reset_rate_buckets(monkeypatch):
    mcp_server._INGEST_RATE_BUCKETS.clear()
    monkeypatch.delenv("MM_INGEST_RATE_LIMIT_PER_MIN", raising=False)
    yield
    mcp_server._INGEST_RATE_BUCKETS.clear()


def _mcp_db(tmp_path):
    if not hasattr(mcp_server, "ingest_rule") or not hasattr(mcp_server, "init_db"):
        pytest.skip("MCP not installed")
    db = str(tmp_path / "rules.db")
    ws = str(tmp_path)
    mcp_server.init_db(db=db, workspace=ws)
    return db, ws


def test_ingest_rule_rejects_secret_in_rationale(tmp_path) -> None:
    """WHY: ingest_rule is an ingest path; a secret in ANY rule field (here the
    rationale) must be firewalled before it ever reaches the store — exactly as
    ingest_claim rejects sensitive text."""
    db, ws = _mcp_db(tmp_path)
    result = mcp_server.ingest_rule(
        trigger="deploying to prod",
        action="run the smoke suite",
        rationale=f"because the api key is {_OPENAI_SECRET}",
        db=db,
        workspace=ws,
    )
    assert result.get("ok") is not True
    assert result.get("code") == "SENSITIVE_INPUT"


def test_ingest_rule_returns_sanitized_text_not_raw(tmp_path) -> None:
    """WHY: the response is logged to the client transcript. Even when a secret
    slips past one field's guard, the echoed `rule` must be the SANITIZED stored
    text (claim.text), never build_rule_fields' raw output, so the DB-at-rest
    redaction isn't undone by leaking the cleartext back to the caller."""
    db, ws = _mcp_db(tmp_path)
    # A db connection string with embedded password is redacted at rest but is
    # not one of the fields-rejected shapes for action prose; assert the echoed
    # rule never contains the cleartext secret.
    secret_url = "postgres://admin:SuperSecretPw123@db.internal:5432/app"
    result = mcp_server.ingest_rule(
        trigger="connecting",
        action="use the pooled connection",
        rationale=f"primary dsn {secret_url}",
        db=db,
        workspace=ws,
    )
    if result.get("ok"):
        assert secret_url not in result["rule"], "raw secret leaked in echoed rule text"
    else:
        # Rejected up front is also acceptable (and preferred) — secret blocked.
        assert result.get("code") == "SENSITIVE_INPUT"


def test_ingest_rule_honors_rate_limit(tmp_path, monkeypatch) -> None:
    """WHY: without the shared rate guard, ingest_rule was an unmetered write
    path an attacker could hammer to flood the store / bypass ingest_claim's
    limit."""
    db, ws = _mcp_db(tmp_path)
    monkeypatch.setenv("MM_INGEST_RATE_LIMIT_PER_MIN", "2")
    agent = "rule-flooder"
    outcomes = [
        mcp_server.ingest_rule(
            trigger=f"event {i}",
            action="do the thing",
            db=db,
            workspace=ws,
            source_agent=agent,
        )
        for i in range(6)
    ]
    assert any(o.get("code") == "RATE_LIMITED" for o in outcomes), "rule ingest must be rate limited"


# --- Finding 4 (LOW): aggregate ingestion must be bounded; source_agent
# rotation must not be an unmetered bypass nor grow the bucket dict forever. ---

def test_rotating_source_agent_is_bounded_by_global_bucket(monkeypatch) -> None:
    """WHY: source_agent is attacker-chosen. A per-agent-only limit lets an
    attacker rotate the key to ingest without bound. A global bucket must cap
    aggregate ingestion across all agents."""
    mcp_server._INGEST_RATE_BUCKETS.clear()
    monkeypatch.setenv("MM_INGEST_RATE_LIMIT_PER_MIN", "5")
    rejected = False
    for i in range(500):
        if mcp_server._check_ingest_rate_limit(f"agent-{i}", now=1000.0) is not None:
            rejected = True
            break
    assert rejected, "global bucket must eventually reject rotated-agent flood"


def test_rate_bucket_dict_is_bounded(monkeypatch) -> None:
    """WHY: an unbounded buckets dict is a memory-exhaustion vector under
    source_agent rotation."""
    mcp_server._INGEST_RATE_BUCKETS.clear()
    monkeypatch.setenv("MM_INGEST_RATE_LIMIT_PER_MIN", "1000000")
    t = 1000.0
    for i in range(mcp_server._MAX_RATE_BUCKETS + 500):
        mcp_server._check_ingest_rate_limit(f"flood-{i}", now=t)
        t += 0.001
    assert len(mcp_server._INGEST_RATE_BUCKETS) <= mcp_server._MAX_RATE_BUCKETS + 1


# --- Finding 3 (MEDIUM): query_for_task read scope must match the write scope
# helper, so a non-clean workspace dirname doesn't read from a different scope
# than ingest writes to. ---

def test_query_for_task_uses_canonical_project_scope(tmp_path, monkeypatch) -> None:
    """WHY: ingest writes under _project_scope (canonicalized slug); if the read
    path used a raw basename, briefings would query a scope that never receives
    writes for dirnames like 'Foo - Copy' or 'whatsappbot-final', silently
    returning empty."""
    if not hasattr(mcp_server, "query_for_task"):
        pytest.skip("MCP not installed")
    monkeypatch.delenv("MEMORYMASTER_DEFAULT_PROJECT_SCOPE", raising=False)
    monkeypatch.delenv("MEMORYMASTER_WORKSPACE", raising=False)
    workspace = tmp_path / "Foo - Copy"
    workspace.mkdir()
    expected = mcp_server._project_scope(str(workspace))
    result = mcp_server.query_for_task(
        task_description="implement the widget",
        db=str(tmp_path / "qft.db"),
        workspace=str(workspace),
    )
    # Read scope must equal the write-side helper's canonical scope.
    assert result.get("scope") == expected
    assert "copy" not in result.get("scope", "").lower()
