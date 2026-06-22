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
]


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
