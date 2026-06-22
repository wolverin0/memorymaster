"""resolve_project behaviour tests with a fake in-memory provider.

Covers evidence-weighted scoring, the memory-first short-circuit, and the
degraded flag when the provider is unavailable.  No Everything install needed:
the provider is a hand-rolled fake satisfying LocalSearchProvider.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.bridges.local_search.provider import LocalSearchProvider, PathHit
from memorymaster.bridges.local_search.redact import collapse_path
from memorymaster.bridges.local_search.resolver import resolve_project
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


class FakeProvider:
    """Minimal LocalSearchProvider returning canned PathHits."""

    def __init__(self, hits: list[PathHit], *, available: bool = True) -> None:
        self._hits = hits
        self._available = available
        self.searched: list[str] = []

    def available(self) -> bool:
        return self._available

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        kind: str = "any",
        whole_name: bool = False,
    ) -> list[PathHit]:
        self.searched.append(query)
        return list(self._hits)


def _service(tmp_path: Path) -> MemoryService:
    svc = MemoryService(tmp_path / "t.db", workspace_root=tmp_path)
    svc.init_db()
    return svc


def _make_project_dir(
    base: Path, name: str, *, git: bool = False, marker: str | None = None
) -> Path:
    proj = base / name
    proj.mkdir(parents=True, exist_ok=True)
    if git:
        (proj / ".git").mkdir(exist_ok=True)
    if marker:
        (proj / marker).write_text("x", encoding="utf-8")
    return proj


@pytest.mark.unit
def test_fake_provider_satisfies_protocol() -> None:
    assert isinstance(FakeProvider([]), LocalSearchProvider)


@pytest.mark.unit
def test_degraded_when_provider_unavailable(tmp_path: Path) -> None:
    """An unavailable provider yields degraded=True and no everything matches."""
    svc = _service(tmp_path)
    provider = FakeProvider([], available=False)

    result = resolve_project("memorymaster", svc=svc, provider=provider, roots=[])

    assert result.degraded is True
    assert result.matches == []
    assert result.best is None
    assert provider.searched == []  # never searched a dead provider


@pytest.mark.unit
def test_scoring_git_repo_and_marker(tmp_path: Path) -> None:
    """A single git repo with a marker file scores slug+git+marker+unambiguous."""
    proj = _make_project_dir(tmp_path, "memorymaster", git=True, marker="AGENTS.md")
    provider = FakeProvider(
        [PathHit(path=str(proj), kind="dir", size=None, modified=None)]
    )
    svc = _service(tmp_path)

    result = resolve_project(
        "memorymaster", svc=svc, provider=provider, roots=[], ingest_threshold=2.0
    )

    assert result.canonical_slug == "memorymaster"
    assert result.best is not None
    # 0.40 slug + 0.20 git + 0.20 marker + 0.20 unambiguous = 1.00
    assert result.best.confidence == pytest.approx(1.00)
    joined = " ".join(result.best.evidence).lower()
    assert "git repo" in joined
    assert "agents.md" in joined
    assert "unambiguous" in joined


@pytest.mark.unit
def test_strict_winner_keeps_full_score_amid_weaker_candidates(tmp_path: Path) -> None:
    """A candidate with stronger evidence wins outright; weak ones don't drag it down.

    This is the regression guard for the original bug: a uniform per-candidate
    ambiguity penalty floored every score to 0 once many substring matches
    appeared. The git repo (0.60) must strictly beat the bare-slug dir (0.40)
    and keep its full intrinsic score.
    """
    a = _make_project_dir(tmp_path / "a", "memorymaster", git=True)
    b = _make_project_dir(tmp_path / "b", "memorymaster")
    provider = FakeProvider(
        [
            PathHit(path=str(a), kind="dir", size=None, modified=None),
            PathHit(path=str(b), kind="dir", size=None, modified=None),
        ]
    )
    svc = _service(tmp_path)

    result = resolve_project(
        "memorymaster", svc=svc, provider=provider, roots=[], ingest_threshold=2.0
    )

    assert len(result.matches) == 2
    assert result.best is not None and result.best.path == str(a)
    # 0.40 slug + 0.20 git, NOT reduced by the presence of the weaker candidate.
    assert result.best.confidence == pytest.approx(0.60)
    assert "clear winner" in " ".join(result.best.evidence).lower()


@pytest.mark.unit
def test_tied_top_candidates_are_damped(tmp_path: Path) -> None:
    """When the top score is TIED (e.g. two identical repos), confidence is damped.

    Genuine ambiguity — we can't tell the real repo from a copy — so the winner's
    confidence is halved, dropping it below a normal ingest threshold.
    """
    a = _make_project_dir(tmp_path / "a", "memorymaster", git=True)
    b = _make_project_dir(tmp_path / "b", "memorymaster", git=True)
    provider = FakeProvider(
        [
            PathHit(path=str(a), kind="dir", size=None, modified=None),
            PathHit(path=str(b), kind="dir", size=None, modified=None),
        ]
    )
    svc = _service(tmp_path)

    result = resolve_project(
        "memorymaster", svc=svc, provider=provider, roots=[], ingest_threshold=2.0
    )

    assert result.best is not None
    # Both intrinsic 0.60, tied -> damped by _AMBIGUITY_DAMP (0.5) -> 0.30.
    assert result.best.confidence == pytest.approx(0.30)
    assert "contested" in " ".join(result.best.evidence).lower()


@pytest.mark.unit
def test_candidates_under_hidden_dirs_are_excluded(tmp_path: Path) -> None:
    """A slug-matching dir living under a hidden ancestor (.cache) is not a candidate."""
    real = _make_project_dir(tmp_path, "memorymaster", git=True, marker="AGENTS.md")
    _make_project_dir(tmp_path / ".cache", "memorymaster")  # hidden ancestor
    provider = FakeProvider(
        [
            PathHit(path=str(tmp_path / ".cache" / "memorymaster"), kind="dir", size=None, modified=None),
            PathHit(path=str(real), kind="dir", size=None, modified=None),
        ]
    )
    svc = _service(tmp_path)

    result = resolve_project(
        "memorymaster", svc=svc, provider=provider, roots=[], ingest_threshold=2.0
    )

    assert len(result.matches) == 1
    assert result.best is not None and result.best.path == str(real)


@pytest.mark.unit
def test_non_matching_basenames_filtered(tmp_path: Path) -> None:
    """Hits whose basename canonicalizes to a different slug are dropped."""
    proj = _make_project_dir(tmp_path, "somethingelse")
    provider = FakeProvider(
        [PathHit(path=str(proj), kind="dir", size=None, modified=None)]
    )
    svc = _service(tmp_path)

    result = resolve_project("memorymaster", svc=svc, provider=provider, roots=[])

    assert result.matches == []
    assert result.best is None


@pytest.mark.unit
def test_memory_first_short_circuits_provider(tmp_path: Path) -> None:
    """A confirmed prior local_path claim wins and the provider is never queried."""
    svc = _service(tmp_path)
    roots = [("projects", str(tmp_path))]
    proj = _make_project_dir(tmp_path, "memorymaster")
    token = collapse_path(roots, str(proj))

    claim = svc.ingest(
        text=f"memorymaster resolves to {token}",
        citations=[CitationInput(source="local-search", locator="memorymaster")],
        claim_type="reference",
        subject="memorymaster",
        predicate="local_path",
        object_value=token,
        scope="project:memorymaster",
        source_agent="local-search",
        confidence=0.9,
    )
    svc.store.apply_status_transition(
        svc.store.get_claim(claim.id, include_citations=False),
        to_status="confirmed",
        reason="test",
        event_type="validator",
    )

    provider = FakeProvider(
        [PathHit(path=str(proj), kind="dir", size=None, modified=None)]
    )
    result = resolve_project(
        "MemoryMaster", svc=svc, provider=provider, roots=roots
    )

    assert provider.searched == []  # memory short-circuit
    assert result.best is not None
    assert result.best.source == "memory"
    assert result.best.confidence == pytest.approx(0.95)
    assert result.best.path == str(proj)  # token expanded back to abspath


@pytest.mark.unit
def test_memory_first_accepts_own_unconfirmed_candidate(tmp_path: Path) -> None:
    """A candidate local_path claim authored by local-search engages memory-first.

    The instant-second-lookup loop must work BEFORE the steward confirms the
    auto-ingested claim — otherwise every repeat call re-scans the disk. We trust
    our own recent resolution (source_agent='local-search') even as a candidate.
    """
    svc = _service(tmp_path)
    roots = [("projects", str(tmp_path))]
    proj = _make_project_dir(tmp_path, "memorymaster")
    token = collapse_path(roots, str(proj))

    # Ingest and DO NOT confirm — stays a candidate.
    svc.ingest(
        text=f"memorymaster resolves to {token}",
        citations=[CitationInput(source="local-search", locator="memorymaster")],
        claim_type="reference",
        subject="memorymaster",
        predicate="local_path",
        object_value=token,
        scope="project:memorymaster",
        source_agent="local-search",
        confidence=0.9,
    )

    provider = FakeProvider(
        [PathHit(path=str(proj), kind="dir", size=None, modified=None)]
    )
    result = resolve_project("memorymaster", svc=svc, provider=provider, roots=roots)

    assert provider.searched == []  # memory short-circuit on our own candidate
    assert result.best is not None and result.best.source == "memory"
    assert result.best.path == str(proj)
