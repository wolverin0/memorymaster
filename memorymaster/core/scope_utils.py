"""Scope derivation utilities (v3.9.0 F3).

Ported from MemPalace v3.3.3's `_wing_from_transcript_path` pattern. The
problem: when a Stop hook (or batch importer) needs to derive the project
scope from a Claude Code session JSONL, the encoded folder name
(``-G--OneDrive-OneDrive-Desktop-Py-Apps-memorymaster``) is lossy — slug
decoding produces ambiguity.

The fix: read the authoritative ``cwd`` field from the transcript JSONL
metadata. Each session record carries the working directory the session was
launched in. Slug decoding stays as a last-resort fallback.

The 2026-04-09 v3.3.1 release patched a related bug
(``_project_scope`` was appending an SHA1 hash suffix unconditionally). This
helper sits one layer up: it tells callers WHICH cwd to feed
``_project_scope``.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

__all__ = [
    "scope_from_cwd",
    "cwd_from_transcript",
    "scope_from_transcript",
    "canonicalize_slug",
    "ancestor_project_scopes",
    "project_scope_variants",
    "PROJECT_ROOT_MARKERS",
    "MAX_ANCESTOR_DEPTH",
]

# A directory carrying agent instructions IS a project root. That is the whole
# rule for how far up the tree a scope reaches, and it is not a heuristic: the
# marker is the thing that makes agents treat the directory as a project in the
# first place.
PROJECT_ROOT_MARKERS = ("CLAUDE.md", "AGENTS.md")
# Bound the walk so a workspace on a strange mount cannot climb to the
# filesystem root collecting scopes.
MAX_ANCESTOR_DEPTH = 4


_SLUG_NORMALIZER_RE = re.compile(r"\s+")

# ---- slug canonicalization (copied verbatim from surfaces/mcp_server.py) ----
# These regexes are the single source of truth; mcp_server.py imports them
# from here (or keeps its own copy for now — both produce identical output).

_SCOPE_SAFE_RE = re.compile(r"[^a-z0-9_-]+")
# Windows / macOS "Copy" artefacts and trailing "(1)"-style numeric variants.
_COPY_SUFFIX_RE = re.compile(
    r"(?:\s*[-_]?\s*copy(?:\s*[-_]?\s*copy)*|\s*\(\d+\)|_copy\d*)\s*$",
    re.IGNORECASE,
)
# Deployment-channel suffixes: whatsappbot-final -> whatsappbot
_CHANNEL_SUFFIX_RE = re.compile(r"-(?:final|prod|production|dev|staging|stage|qa|test)$")


def canonicalize_slug(dirname: str) -> str:
    """Canonicalize a workspace dirname into a stable project slug.

    Rules:
      1. Lowercase + strip whitespace.
      2. Strip Windows/macOS ``- Copy``, ``- Copy - Copy``, ``(1)``, ``_copy``
         suffixes (loop until stable).
      3. Replace non-slug chars with ``-``; strip leading/trailing ``-._``.
      4. Strip deployment-channel suffix (``-final``, ``-prod``, …).

    This is the same logic as ``_canonicalize_slug`` in
    ``memorymaster.surfaces.mcp_server``; kept here as the shared utility so
    the local-search resolver and other callers do not import from surfaces.
    """
    base = (dirname or "").strip().lower()
    prev = None
    while prev != base:
        prev = base
        base = _COPY_SUFFIX_RE.sub("", base).strip()
    if not base:
        return "workspace"
    slug = _SCOPE_SAFE_RE.sub("-", base).strip("-._") or "workspace"
    folded = _CHANNEL_SUFFIX_RE.sub("", slug)
    return folded or slug


def project_scope_variants(dirname: str) -> list[str]:
    """The canonical scope for a directory, plus the pre-fold literal if different.

    ``canonicalize_slug`` folds deployment-channel suffixes so that
    ``whatsappbot-final`` and ``whatsappbot-prod`` share one scope — correct, and
    it prevents fragmentation on the WRITE side when the scope is derived.

    But ingest with an explicit ``scope`` argument stores the string verbatim,
    and callers pass the directory name they see. That asymmetry — folded on
    read, literal on write — left 3901 live claims in ``project:whatsappbot-final``
    that no workspace could ever resolve to (measured 2026-08-21).

    Reading both variants closes that gap without rewriting a single row. It is
    deliberately read-only: writes still resolve to exactly one canonical scope,
    so this does not create new fragmentation, it just stops the existing
    fragmentation from hiding data.
    """
    canonical = canonicalize_slug(dirname)
    base = (dirname or "").strip().lower()
    prev = None
    while prev != base:
        prev = base
        base = _COPY_SUFFIX_RE.sub("", base).strip()
    literal = _SCOPE_SAFE_RE.sub("-", base).strip("-._") or "workspace"
    scopes = [f"project:{canonical}"]
    if literal != canonical:
        scopes.append(f"project:{literal}")
    return scopes


def ancestor_project_scopes(
    workspace: str | os.PathLike[str] | None,
    *,
    max_depth: int = MAX_ANCESTOR_DEPTH,
) -> list[str]:
    """Project scopes of the ancestor directories that are themselves projects.

    A workspace nested inside a larger tree belongs to two projects at once, and
    until 2026-08-21 recall only knew about the inner one. The reading allowlist
    was derived from the workspace directory alone, so a session in
    ``Py Apps/infra`` saw ``project:infra`` and nothing above it — while the
    tree's own instructions told every session under it to INGEST into
    ``project:py-apps``. Measured that day: 9 of 10 fleet panes could not read
    the scope they were being told to write to, across roughly 3000 live claims.
    Nothing failed; recall returned the pane's own scope and read as healthy.

    The rule for how far up to reach is not a heuristic. A directory carrying
    ``CLAUDE.md`` or ``AGENTS.md`` is a project root *because* that marker is
    what makes agents treat it as one. ``Py Apps`` has the marker and stops the
    walk; ``Desktop`` above it does not.

    Returns canonical ``project:<slug>`` strings, nearest ancestor first, never
    including the workspace itself. Unreadable or missing paths return ``[]``
    rather than raising — this feeds a read path, and a scope lookup must not be
    able to break a query.
    """
    raw = str(workspace or "").strip()
    if not raw:
        return []
    try:
        current = Path(raw).resolve()
    except (OSError, ValueError):
        return []

    scopes: list[str] = []
    seen: set[str] = set()
    for parent in list(current.parents)[:max_depth]:
        try:
            is_root = any((parent / marker).is_file() for marker in PROJECT_ROOT_MARKERS)
        except OSError:
            break
        if not is_root:
            continue
        scope = f"project:{canonicalize_slug(parent.name)}"
        if scope not in seen:
            seen.add(scope)
            scopes.append(scope)
    return scopes


def scope_from_cwd(cwd: str | os.PathLike[str] | None) -> str:
    """Derive a ``project:<slug>`` scope from a CWD path.

    - ``cwd is None`` or empty → ``"global"``
    - Non-empty cwd → ``project:<lowercased-basename-with-spaces-as-dashes>``

    Spaces, mixed case, and trailing separators are normalised. This is the
    same shape the deployed Stop hook produces, lifted into a re-usable
    helper so other callers (verbatim_store, dream-ingest, batch importers)
    can match it byte-for-byte.
    """
    if not cwd:
        return "global"
    name = Path(str(cwd)).name
    if not name:
        return "global"
    slug = _SLUG_NORMALIZER_RE.sub("-", name.strip().lower())
    if not slug:
        return "global"
    return f"project:{slug}"


def cwd_from_transcript(transcript_path: str | os.PathLike[str] | None) -> str | None:
    """Extract the authoritative ``cwd`` from a Claude Code session JSONL.

    Walks the file line-by-line until a record with a non-empty top-level
    ``cwd`` field is found. Returns ``None`` if the file is missing,
    unreadable, or contains no ``cwd`` records.

    The JSONL format used by Claude Code stores ``cwd`` on every conversation
    record, but typical files have it on the very first line — so the walk
    short-circuits quickly. We do not parse the entire file.
    """
    if not transcript_path:
        return None
    p = Path(str(transcript_path))
    if not p.is_file():
        return None
    try:
        with p.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                cwd = record.get("cwd") if isinstance(record, dict) else None
                if isinstance(cwd, str) and cwd.strip():
                    return cwd.strip()
    except OSError:
        return None
    return None


def scope_from_transcript(
    transcript_path: str | os.PathLike[str] | None,
    *,
    fallback_cwd: str | os.PathLike[str] | None = None,
) -> str:
    """Resolve a project scope using the transcript's authoritative cwd, with fallback.

    Priority chain:
    1. ``cwd`` extracted from the transcript JSONL metadata.
    2. ``fallback_cwd`` argument (typically what the hook received via
       stdin, or ``os.getcwd()``).
    3. ``"global"``.

    Returns a ``project:<slug>`` string from ``scope_from_cwd``.
    """
    transcript_cwd = cwd_from_transcript(transcript_path)
    if transcript_cwd:
        return scope_from_cwd(transcript_cwd)
    return scope_from_cwd(fallback_cwd)
