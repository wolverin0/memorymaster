"""Privacy-gate tests for resolve_project auto-ingest (intent-anchored).

WHY these matter (not just WHAT they do): the resolver writes resolved paths
back into governed memory.  On Windows almost every project path is
``C:\\Users\\<name>\\...`` — a username leak.  The gate is:
``collapse_path -> scan_text_for_findings -> skip-on-finding -> else ingest``.

If the scan guard is ever removed, these tests MUST fail.  The negative test
proves a raw username path is NOT stored; the positive test proves a properly
collapsed token IS stored and re-findable, so the gate is not just blanket-deny.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from memorymaster.bridges.local_search.provider import PathHit
from memorymaster.bridges.local_search.resolver import resolve_project
from memorymaster.core.security import scan_text_for_findings
from memorymaster.core.service import MemoryService


class FakeProvider:
    """LocalSearchProvider returning a single canned dir hit."""

    def __init__(self, path: str) -> None:
        self._path = path

    def available(self) -> bool:
        return True

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        kind: str = "any",
        whole_name: bool = False,
    ) -> list[PathHit]:
        return [PathHit(path=self._path, kind="dir", size=None, modified=None)]


def _service(tmp_path: Path) -> MemoryService:
    svc = MemoryService(tmp_path / "t.db", workspace_root=tmp_path)
    svc.init_db()
    return svc


def _stored_local_path_claims(svc: MemoryService, slug: str) -> list:
    """All local_path claims physically WRITTEN to the store for project:<slug>.

    Uses ``store.list_claims`` (not ``svc.query``) on purpose: the service's own
    recall filter independently HIDES sensitive claims from ``query``, so a
    query-based check would pass even if a leaky claim were written.  We must
    assert against the raw store so the negative test is anchored to the
    resolver's scan guard — the only thing that prevents the write.
    """
    rows = svc.store.list_claims(limit=100, scope_allowlist=[f"project:{slug}"])
    return [c for c in rows if getattr(c, "predicate", None) == "local_path"]


def _refindable_local_path_claims(svc: MemoryService, slug: str) -> list:
    """local_path claims re-findable via the recall path (svc.query)."""
    rows = svc.query(
        slug,
        limit=50,
        include_stale=True,
        include_conflicted=True,
        include_candidates=True,
        scope_allowlist=[f"project:{slug}"],
    )
    return [c for c in rows if getattr(c, "predicate", None) == "local_path"]


@pytest.mark.unit
def test_username_path_is_not_ingested(tmp_path: Path) -> None:
    """A path that collapses to a raw home-dir/<name> token must NOT be stored.

    No root matches the candidate, so collapse_path returns it unchanged; the
    scan then flags the username and the gate must abort the ingest. This is the
    test that fails if the scan_text_for_findings guard is removed.
    """
    # OS-native separator so Path(...).name == "memorymaster" on BOTH platforms
    # (POSIX does not split on backslashes — a Windows literal would make the
    # candidate's basename the whole string and drop it before the gate runs).
    leaky = (
        r"C:\Users\victim\projects\memorymaster"
        if os.name == "nt"
        else "/home/victim/projects/memorymaster"
    )
    # Sanity: the scanner really does flag this (anchors the test's premise).
    assert scan_text_for_findings(f"memorymaster resolves to {leaky}")

    svc = _service(tmp_path)
    provider = FakeProvider(leaky)

    # roots=[] => no root matches => collapse_path leaves the raw username path.
    result = resolve_project(
        "memorymaster",
        svc=svc,
        provider=provider,
        roots=[],
        ingest_threshold=0.0,  # force the ingest branch; only the scan guard stops it
    )

    assert result.best is not None  # resolver still ANSWERS
    # Anchored to the raw store: nothing was written. If the scan guard is
    # removed, svc.ingest WILL persist the leaky claim and this list is non-empty.
    assert _stored_local_path_claims(svc, "memorymaster") == []


@pytest.mark.unit
def test_bare_ip_path_is_not_ingested(tmp_path: Path) -> None:
    """A path token carrying a BARE IPv4 must NOT be stored.

    The shared filter (``scan_text_for_findings``) deliberately allows bare
    private IPv4 (only IP+port is flagged), so this leak vector slips past the
    general gate. The resolver applies a stricter, path-token-specific guard
    (``_IPV4_RE``). This test fails if that extra guard is removed.
    """
    leaky = "/srv/10.0.0.5/memorymaster"  # basename canonicalizes to the slug
    # Premise: the SHARED scanner does NOT catch a bare IP — only our extra
    # resolver guard does. If this assert ever flips, the general filter changed
    # and this test's rationale should be revisited.
    assert not scan_text_for_findings(f"memorymaster resolves to {leaky}")

    svc = _service(tmp_path)
    provider = FakeProvider(leaky)

    result = resolve_project(
        "memorymaster",
        svc=svc,
        provider=provider,
        roots=[],  # no root matches => token keeps the bare IP
        ingest_threshold=0.0,
    )

    assert result.best is not None  # resolver still ANSWERS
    assert _stored_local_path_claims(svc, "memorymaster") == []


@pytest.mark.unit
def test_collapsed_token_is_ingested_and_refindable(tmp_path: Path) -> None:
    """A clean root-relative token IS ingested and re-findable via the service."""
    proj = tmp_path / "memorymaster"
    proj.mkdir(parents=True, exist_ok=True)
    roots = [("projects", str(tmp_path))]

    svc = _service(tmp_path)
    provider = FakeProvider(str(proj))

    result = resolve_project(
        "memorymaster",
        svc=svc,
        provider=provider,
        roots=roots,
        ingest_threshold=0.0,
    )

    assert result.best is not None
    claims = _stored_local_path_claims(svc, "memorymaster")
    assert len(claims) == 1
    stored = claims[0]
    token = stored.object_value
    assert token == "projects/memorymaster"
    # No raw username / drive prefix leaked into the stored token.
    assert "Users" not in token
    assert "C:" not in token
    # Re-findable by a fresh recall query (proves opt-B beat the sensitive-flag:
    # a clean token is NOT marked sensitive, so query_memory surfaces it).
    assert any(
        getattr(c, "object_value", None) == "projects/memorymaster"
        for c in _refindable_local_path_claims(svc, "memorymaster")
    )
