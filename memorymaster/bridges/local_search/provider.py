"""Shared DTOs and LocalSearchProvider Protocol for the local filesystem bridge.

All search backends (EverythingProvider, future plocate/fd/mdfind providers)
implement LocalSearchProvider so callers stay backend-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple, Protocol, runtime_checkable

__all__ = [
    "PathHit",
    "ResolveMatch",
    "ResolveResult",
    "LocalSearchProvider",
]


class PathHit(NamedTuple):
    """A single raw hit returned by a search backend.

    Attributes:
        path:     Absolute filesystem path (or root-relative token after redaction).
        kind:     ``"file"`` | ``"dir"`` | ``"any"`` — provider best-effort.
        size:     File size in bytes; ``None`` when unavailable or kind is ``"dir"``.
        modified: Last-modified timestamp as a POSIX float; ``None`` if unavailable.
    """

    path: str
    kind: str
    size: int | None
    modified: float | None


@dataclass(frozen=True)
class ResolveMatch:
    """A single candidate resolution for a project alias.

    Attributes:
        path:       Absolute path to the project root (not yet redacted).
        confidence: Score in [0.0, 1.0] — sum of evidence weights, capped at 1.0.
        evidence:   Human-readable strings explaining why this match was scored.
        source:     ``"memory"`` (recalled from prior claim) or
                    ``"everything"`` (found by the search backend this call).
    """

    path: str
    confidence: float
    evidence: list[str]
    source: str  # "memory" | "everything"


@dataclass(frozen=True)
class ResolveResult:
    """Full result of a project-alias resolution attempt.

    Attributes:
        query:          The original alias string passed by the caller.
        canonical_slug: Normalised slug derived from *query*.
        matches:        All candidates found, ordered by confidence descending.
        best:           Highest-confidence match, or ``None`` if none found.
        degraded:       ``True`` when the search backend was unavailable and only
                        memory claims (if any) were consulted.
    """

    query: str
    canonical_slug: str
    matches: list[ResolveMatch]
    best: ResolveMatch | None
    degraded: bool


@runtime_checkable
class LocalSearchProvider(Protocol):
    """Protocol that every path-search backend must satisfy.

    Implementations MUST:
    - Never raise to the caller — return ``[]`` / ``False`` on any error.
    - Never use ``shell=True`` in subprocess calls.
    - Respect ``limit`` and ``kind`` filters on a best-effort basis.
    """

    def available(self) -> bool:
        """Return ``True`` iff the backend is ready to serve queries."""
        ...

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        kind: str = "any",
        whole_name: bool = False,
    ) -> list[PathHit]:
        """Search for paths matching *query*.

        Args:
            query:      Free-text or filename fragment to search for.
            limit:      Maximum number of results to return (best-effort).
            kind:       ``"file"``, ``"dir"``, or ``"any"``.
            whole_name: When ``True``, match the *whole* file/dir name rather
                        than a substring (portable concept: Everything ``wfn:``,
                        plocate/fd basename-exact). Used by the project resolver
                        to avoid drowning in substring matches.

        Returns:
            List of :class:`PathHit` objects; empty list on any error.
        """
        ...
