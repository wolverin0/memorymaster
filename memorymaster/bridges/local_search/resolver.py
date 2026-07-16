"""resolve_project — fuzzy project alias -> canonical on-disk path.

Memory-first, Everything-second resolution with explainable, evidence-weighted
confidence.  Confident, non-sensitive matches are written back as governed
``reference`` claims so the next lookup is memory-only and survives across
sessions and CLIs.

The auto-ingest path is privacy-critical: every candidate path is collapsed to
a root-relative token (``redact.collapse_path``) and then scanned with
``core.security.scan_text_for_findings``.  Any finding (username, IP, token,
…) aborts the ingest entirely — the resolver still *answers*, it just refuses
to *write*.  See LOCALFS-SPEC.md §4 / §10.
"""
from __future__ import annotations

import re
from pathlib import Path

from memorymaster.bridges.local_search.provider import (
    LocalSearchProvider,
    PathHit,
    ResolveMatch,
    ResolveResult,
)
from memorymaster.bridges.local_search.redact import (
    collapse_path,
    expand_path,
    load_roots,
)
from memorymaster.core.scope_utils import canonicalize_slug
from memorymaster.core.security import scan_text_for_findings

__all__ = ["resolve_project"]

# Evidence weights (LOCALFS-SPEC.md §10). Confidence is the sum, capped at 1.0.
_W_SLUG_MATCH = 0.40
_W_GIT_REPO = 0.20
_W_MARKER_FILE = 0.20
_W_UNAMBIGUOUS = 0.20
# When the top score is TIED by 2+ candidates we genuinely can't disambiguate,
# so we damp the winner's confidence (multiplier). A *strict* winner — e.g. the
# real repo (slug+git+marker=0.80) beating bare-slug caches (0.40) — keeps its
# full score even amid dozens of junk matches. (A uniform per-candidate penalty
# was the original bug: with N=50 substring hits it floored EVERY score to 0.)
_AMBIGUITY_DAMP = 0.5

# Whole-name + bounded fan-out: ask the backend for exact-name dir matches so a
# generic substring ("memorymaster" -> 261 hits) can't truncate the real repo
# out of the result window.
_EVERYTHING_LIMIT = 200

_MEMORY_CONFIDENCE = 0.95
_DEFAULT_REMEMBER_THRESHOLD = 0.85
_MARKER_FILES = ("AGENTS.md", "CLAUDE.md", "pyproject.toml", "package.json")

# A bare IPv4 in a path token is a leak vector that scan_text_for_findings
# deliberately lets through — the general filter only flags IP+port
# (core/security.py:52). Path tokens get stricter treatment: refuse to
# remember any IPv4-shaped token at all.
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

# Type alias for the memory-first roots argument (list of (name, abspath)).
_Roots = list[tuple[str, str]]


def _is_git_repo(path: str) -> bool:
    """True iff *path* contains a ``.git`` entry (dir or file)."""
    try:
        return (Path(path) / ".git").exists()
    except OSError:
        return False


def _marker_files(path: str) -> list[str]:
    """Return the marker filenames present directly under *path*."""
    found: list[str] = []
    for marker in _MARKER_FILES:
        try:
            if (Path(path) / marker).is_file():
                found.append(marker)
        except OSError:
            continue
    return found


def _mtime(path: str) -> float:
    """Last-modified time, or 0.0 if the path can't be stat'd.

    A deleted dir that lingers in a stale search index returns 0.0, so it always
    loses a recency tiebreak to a path that actually exists on disk.
    """
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def _under_hidden_dir(path: str) -> bool:
    """True if *path* itself or any ancestor is a hidden (dot-prefixed) dir.

    A project dir living under ``.gemini`` / ``.memorymaster`` / ``.cache`` is a
    tool cache or snapshot, not the canonical project — exclude it as a
    candidate. (The drive anchor like ``C:\\`` never starts with a dot.)
    """
    try:
        parts = Path(path).parts
    except (OSError, ValueError):
        return False
    return any(part.startswith(".") for part in parts)


def _memory_match(
    slug: str,
    *,
    svc: object,
    roots: _Roots,
) -> ResolveMatch | None:
    """Look for a prior ``local_path`` claim in ``project:<slug>``.

    Accepts a ``confirmed`` claim (steward-validated) OR a ``candidate`` claim
    this resolver authored itself (``source_agent == "local-search"``) — the
    latter so the instant-second-lookup loop engages immediately, before the
    steward's async confirmation cycle. Returns a high-confidence
    ``source="memory"`` match (token expanded back to an absolute path), or
    ``None`` when nothing usable is found.
    """
    scope = f"project:{slug}"
    try:
        claims = svc.query(  # type: ignore[attr-defined]
            slug,
            limit=20,
            include_candidates=True,
            scope_allowlist=[scope],
        )
    except (AttributeError, ValueError, RuntimeError):
        return None
    for claim in claims:
        predicate = getattr(claim, "predicate", None)
        status = getattr(claim, "status", None)
        token = getattr(claim, "object_value", None)
        source_agent = getattr(claim, "source_agent", None)
        if predicate != "local_path" or not token:
            continue
        trusted = status == "confirmed" or (
            status == "candidate" and source_agent == "local-search"
        )
        if not trusted:
            continue
        abspath = expand_path(roots, token)
        return ResolveMatch(
            path=abspath,
            confidence=_MEMORY_CONFIDENCE,
            evidence=[f"remembered as {token}"],
            source="memory",
        )
    return None


def _score_candidate(hit: PathHit, *, slug: str) -> ResolveMatch:
    """Score one candidate on its INTRINSIC evidence only (slug+git+marker).

    Cross-candidate effects (unambiguous bonus / contested damping) are applied
    once, to the winner, in :func:`_disambiguate` — never per-candidate, so a
    field of junk matches can't drag down a clearly-evidenced repo.
    """
    confidence = _W_SLUG_MATCH
    evidence = [f"dirname canonicalizes to '{slug}'"]

    if _is_git_repo(hit.path):
        confidence += _W_GIT_REPO
        evidence.append("is a git repo")

    markers = _marker_files(hit.path)
    if markers:
        confidence += _W_MARKER_FILE
        evidence.append("has marker file " + ", ".join(markers))

    return ResolveMatch(
        path=hit.path,
        confidence=min(1.0, confidence),
        evidence=evidence,
        source="everything",
    )


def _disambiguate(best: ResolveMatch, matches: list[ResolveMatch]) -> ResolveMatch:
    """Resolve the winning match from the intrinsic-scored candidate list.

    - exactly 1 candidate -> unambiguous bonus (+_W_UNAMBIGUOUS).
    - strict winner       -> keep full intrinsic score.
    - top score tied      -> return the most-recently-modified (active project >
                             stale copy), but DAMP confidence so a recency *guess*
                             stays below a normal ingest threshold — we answer,
                             we don't auto-remember a coin-flip.
    """
    n = len(matches)
    if n == 1:
        return ResolveMatch(
            path=best.path,
            confidence=min(1.0, best.confidence + _W_UNAMBIGUOUS),
            evidence=[*best.evidence, "unambiguous (exactly 1 candidate)"],
            source=best.source,
        )
    tied = [m for m in matches if m.confidence == best.confidence]
    if len(tied) == 1:
        return ResolveMatch(
            path=best.path,
            confidence=best.confidence,
            evidence=[*best.evidence, f"clear winner over {n - 1} other candidate(s)"],
            source=best.source,
        )
    winner = max(tied, key=lambda m: _mtime(m.path))
    return ResolveMatch(
        path=winner.path,
        confidence=round(best.confidence * _AMBIGUITY_DAMP, 4),
        evidence=[
            *winner.evidence,
            f"contested: {len(tied)} candidates tied; chose most-recently-modified "
            "(not auto-remembered)",
        ],
        source=winner.source,
    )


def _everything_matches(
    alias: str,
    slug: str,
    *,
    provider: LocalSearchProvider,
) -> list[ResolveMatch]:
    """Search the provider and score directory candidates whose basename canonicalizes to *slug*."""
    try:
        hits = provider.search(
            alias, kind="dir", whole_name=True, limit=_EVERYTHING_LIMIT
        )
    except (OSError, ValueError, RuntimeError):
        return []
    candidates = [
        hit
        for hit in hits
        if not _under_hidden_dir(hit.path)
        and canonicalize_slug(Path(hit.path).name) == slug
    ]
    return [_score_candidate(hit, slug=slug) for hit in candidates]


def _maybe_ingest(
    best: ResolveMatch,
    slug: str,
    *,
    svc: object,
    roots: _Roots,
    ingest_threshold: float,
) -> bool:
    """Auto-ingest gate (the privacy crown jewel).

    Collapses the path to a token, scans it for sensitive findings, and only
    ingests when the scan is clean.  Any finding aborts the write entirely.
    """
    if best.source == "memory":
        return False
    if best.confidence < ingest_threshold:
        return False

    token = collapse_path(roots, best.path)
    text = f"{slug} resolves to {token}"
    if scan_text_for_findings(text):
        # A username / IP+port / secret slipped through collapse — never store it.
        return False
    if _IPV4_RE.search(text):
        # Stricter-than-general guard: the shared filter allows bare private
        # IPv4, but a path token must never carry one. Answer, don't remember.
        return False

    from memorymaster.core.models import CitationInput

    try:
        svc.ingest(  # type: ignore[attr-defined]
            text=text,
            citations=[CitationInput(source="local-search", locator=slug)],
            claim_type="reference",
            subject=slug,
            predicate="local_path",
            object_value=token,
            scope=f"project:{slug}",
            source_agent="local-search",
            confidence=best.confidence,
        )
        return True
    except (ValueError, RuntimeError, AttributeError):
        # Ingest is best-effort; a write failure must never break resolution.
        return False


def resolve_project(
    alias: str,
    *,
    svc: object,
    provider: LocalSearchProvider,
    ingest_threshold: float = _DEFAULT_REMEMBER_THRESHOLD,
    roots: _Roots | None = None,
    remember: bool = False,
) -> ResolveResult:
    """Resolve a fuzzy project *alias* to canonical on-disk path(s).

    Args:
        alias:            Free-text project name (e.g. ``"MemoryMaster"``).
        svc:              MemoryService-like object exposing ``query`` + ``ingest``.
        provider:         A LocalSearchProvider (EverythingProvider or a fake).
        ingest_threshold: Minimum confidence to persist a confirmed fresh match.
        roots:            Root registry from ``load_roots`` (loaded if ``None``).
        remember:         Explicit confirmation to persist a fresh match. The
                          default is read-only.

    Returns:
        A :class:`ResolveResult` with all matches, the best match (if any),
        and a ``degraded`` flag when the provider is unavailable.
    """
    if roots is None:
        roots = load_roots()

    slug = canonicalize_slug(alias)
    matches: list[ResolveMatch] = []

    memory = _memory_match(slug, svc=svc, roots=roots)
    if memory is not None:
        matches.append(memory)

    degraded = not provider.available()

    if memory is None and not degraded:
        matches.extend(
            _everything_matches(alias, slug, provider=provider)
        )

    matches.sort(key=lambda m: m.confidence, reverse=True)
    best = matches[0] if matches else None

    # `best` is the resolver's verdict; it may be confidence-adjusted (and, when
    # contested, point at the most-recently-modified candidate) relative to the
    # raw intrinsic ranking left in `matches`.
    if best is not None and best.source == "everything":
        best = _disambiguate(best, matches)

    remembered = False
    if remember and best is not None:
        remembered = _maybe_ingest(
            best,
            slug,
            svc=svc,
            roots=roots,
            ingest_threshold=ingest_threshold,
        )

    return ResolveResult(
        query=alias,
        canonical_slug=slug,
        matches=matches,
        best=best,
        degraded=degraded,
        remembered=remembered,
    )
