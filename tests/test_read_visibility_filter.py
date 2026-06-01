"""Regression tests for per-agent read visibility (read-visibility cluster).

WHY these matter:

1. query_rows must NOT leak one agent's PRIVATE claims to another agent,
   regardless of retrieval_mode. Before the fix the legacy path applied the
   per-agent visibility filter but the HYBRID path did not, so an attacker
   could pick `retrieval_mode="hybrid"` and read another agent's private
   memory — a path-dependent cross-agent data leak.

2. `query --as-of <ts>` must not print sensitive-visibility claims in
   plaintext unless --allow-sensitive was actually granted. The non-as-of
   path already excludes them; the as_of branch bypassed that gate.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.models import CitationInput
from memorymaster.service import MemoryService


QUERY_TOKEN = "visibilityregressiontoken"


def _service(tmp_path: Path, monkeypatch) -> MemoryService:
    monkeypatch.delenv("QDRANT_URL", raising=False)
    svc = MemoryService(str(tmp_path / "memory.db"), workspace_root=tmp_path)
    svc.init_db()
    return svc


def _ingest(svc: MemoryService, text: str, *, agent: str, visibility: str = "public"):
    return svc.ingest(
        text=text,
        citations=[CitationInput(source="synthetic-test", locator="t", excerpt=text)],
        scope="project:foo",
        claim_type="fact",
        source_agent=agent,
        visibility=visibility,
    )


def _texts(rows) -> set[str]:
    return {r["claim"].text for r in rows}


@pytest.mark.parametrize("retrieval_mode", ["legacy", "hybrid"])
def test_other_agents_private_claim_not_leaked(tmp_path, monkeypatch, retrieval_mode):
    """A private claim authored by agentA must never reach agentB — on EITHER
    retrieval path. This anchors the requirement (no cross-agent private leak),
    not the implementation: switching modes must not change who can read what.
    """
    svc = _service(tmp_path, monkeypatch)
    public = f"{QUERY_TOKEN} shared public note"
    a_private = f"{QUERY_TOKEN} agentA secret note"
    _ingest(svc, public, agent="agentA", visibility="public")
    _ingest(svc, a_private, agent="agentA", visibility="private")

    rows = svc.query_rows(
        query_text=QUERY_TOKEN,
        retrieval_mode=retrieval_mode,
        requesting_agent="agentB",
        include_candidates=True,
        limit=20,
    )
    got = _texts(rows)
    assert a_private not in got, f"{retrieval_mode}: agentA private leaked to agentB"
    assert public in got


@pytest.mark.parametrize("retrieval_mode", ["legacy", "hybrid"])
def test_owner_sees_own_private_claim(tmp_path, monkeypatch, retrieval_mode):
    """The authoring agent must still retrieve its own private claim on both
    paths — the filter must not over-block the owner.
    """
    svc = _service(tmp_path, monkeypatch)
    a_private = f"{QUERY_TOKEN} agentA secret note"
    _ingest(svc, a_private, agent="agentA", visibility="private")

    rows = svc.query_rows(
        query_text=QUERY_TOKEN,
        retrieval_mode=retrieval_mode,
        requesting_agent="agentA",
        include_candidates=True,
        limit=20,
    )
    assert a_private in _texts(rows)


def test_as_of_hides_sensitive_visibility_without_allow_flag(tmp_path, monkeypatch):
    """`query --as-of` without --allow-sensitive must not print sensitive
    claims, matching the non-as-of path. Anchors the privacy requirement, not
    the printing mechanics.
    """
    import argparse

    from memorymaster import cli_handlers_basic

    svc = _service(tmp_path, monkeypatch)
    public = f"{QUERY_TOKEN} as-of public"
    secret = f"{QUERY_TOKEN} as-of sensitive"
    _ingest(svc, public, agent="agentA", visibility="public")
    _ingest(svc, secret, agent="agentA", visibility="sensitive")

    # Capture printed claim objects by patching print_claim.
    printed = []
    monkeypatch.setattr(cli_handlers_basic, "print_claim", lambda c: printed.append(c))

    args = argparse.Namespace(
        allow_sensitive=False,
        as_of="2999-01-01T00:00:00Z",
        json_output=False,
    )
    rc = cli_handlers_basic._handle_query(args, svc, parser=None, effective_db="x")
    assert rc == 0
    printed_visibilities = {(c.visibility or "public").strip().lower() for c in printed}
    assert "sensitive" not in printed_visibilities, (
        "as_of printed a sensitive-visibility claim without --allow-sensitive"
    )
    printed_texts = {c.text for c in printed}
    assert secret not in printed_texts
    assert public in printed_texts


def test_as_of_shows_sensitive_when_allow_granted(tmp_path, monkeypatch):
    """With the bypass enabled and --allow-sensitive, the as_of branch should
    still surface sensitive claims (we don't over-filter when access is granted).
    """
    import argparse

    from memorymaster import cli_handlers_basic
    from memorymaster import security as security_mod

    svc = _service(tmp_path, monkeypatch)
    secret = f"{QUERY_TOKEN} as-of sensitive"
    _ingest(svc, secret, agent="agentA", visibility="sensitive")

    # Force the resolved decision to True regardless of env config.
    monkeypatch.setattr(
        cli_handlers_basic, "resolve_allow_sensitive_access", lambda **kw: True
    )
    printed = []
    monkeypatch.setattr(cli_handlers_basic, "print_claim", lambda c: printed.append(c))

    args = argparse.Namespace(
        allow_sensitive=True,
        as_of="2999-01-01T00:00:00Z",
        json_output=False,
    )
    rc = cli_handlers_basic._handle_query(args, svc, parser=None, effective_db="x")
    assert rc == 0
    assert secret in {c.text for c in printed}
    _ = security_mod  # imported to document the access-control surface under test
