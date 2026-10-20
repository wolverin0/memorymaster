"""Scope derivation utilities (v3.9.0 F3).

Ported from MemPalace v3.3.3's `_wing_from_transcript_path` pattern. The
problem: when a Stop hook (or batch importer) needs to derive the project
scope from a Claude Code session JSONL, the encoded folder name
(``-G--OneDrive-OneDrive-Desktop-Py-Apps-memorymaster``) is lossy — slug
decoding produces ambiguity.

The fix: read the authoritative ``cwd`` field from the transcript JSONL
metadata. Each session record carries the working directory the session was
launched in. Slug decoding stays as a last-resort fallback.

Project identity comes from Git's common directory for linked worktrees;
operator folder aliases override it. Capture, session bindings and surfaces
share this resolution rather than deriving disposable checkout scopes.
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


# Shared slug canonicalization; surfaces import these rules.

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


SCOPE_ALIASES_ENV = "MEMORYMASTER_SCOPE_ALIASES"
_ALIAS_CACHE: dict[str, object] = {"key": None, "map": {}}


def _scope_aliases() -> dict[str, str]:
    """Folder slug -> project scope, from ~/.memorymaster/scope-aliases.json (operator-owned).

    Re-read when the file changes; a missing or broken file means no aliases.
    """
    path = Path(os.environ.get(SCOPE_ALIASES_ENV) or Path.home() / ".memorymaster" / "scope-aliases.json")
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return {}
    if _ALIAS_CACHE["key"] != key:
        try:
            raw = json.loads(path.read_text(encoding="utf-8")).get("aliases", {})
        except (OSError, ValueError, AttributeError):
            raw = {}
        _ALIAS_CACHE["map"] = {
            str(folder).strip().lower(): target.strip()
            for folder, target in (raw.items() if isinstance(raw, dict) else [])
            if isinstance(target, str) and target.strip().startswith("project:")
        }
        _ALIAS_CACHE["key"] = key
    return _ALIAS_CACHE["map"]  # type: ignore[return-value]


def _git_project_root(current: Path) -> tuple[Path, Path] | None:
    """Read Git's gitdir/commondir pointers without launching a hook-time process.

    Gitfile paths are relative to the checkout; commondir paths are relative
    to the per-worktree gitdir. An ordinary gitfile (e.g. a submodule) with no
    commondir still belongs to its own checkout, not the metadata directory.
    Return both checkout and common project roots so checkout aliases also
    apply when a session starts in a subdirectory.
    """
    ceilings = {
        Path(raw).resolve() for raw in os.environ.get("GIT_CEILING_DIRECTORIES", "").split(os.pathsep) if raw
    }
    candidates = (current,) if current in ceilings else (current, *current.parents)
    for candidate in candidates:
        if candidate != current and candidate in ceilings:
            break
        dotgit = candidate / ".git"
        try:
            if dotgit.is_dir():
                gitdir = dotgit
            elif dotgit.is_file():
                pointer = dotgit.read_text(encoding="utf-8").strip()
                if not pointer.startswith("gitdir:") or not pointer[7:].strip():
                    return None
                gitdir = (candidate / pointer[7:].strip()).resolve()
                if not gitdir.is_dir():
                    return None
            else:
                continue
            commondir = gitdir / "commondir"
            if not commondir.is_file():
                return candidate, candidate
            pointer = commondir.read_text(encoding="utf-8").strip()
            if not pointer:
                return None
            common = (gitdir / pointer).resolve()
            if not common.is_dir():
                return None
            # Linked worktrees share the main checkout's .git directory.
            # Bare repositories conventionally use <project>.git instead.
            if common.name.lower() == ".git":
                return candidate, common.parent
            project = common.with_suffix("") if common.suffix.lower() == ".git" else common
            return candidate, project
        except (OSError, ValueError, UnicodeError):
            return None
    return None


def scope_from_cwd(cwd: str | os.PathLike[str] | None) -> str:
    """Resolve the project once for capture, hooks, session bindings and MCP.

    Explicit folder aliases win. Otherwise the nearest Git checkout's common
    directory identifies the project, including worktrees with arbitrary
    names or locations. Non-Git folders keep basename scope derivation;
    multi-repository collection roots and empty paths use global scope.
    """
    raw = str(cwd or "").strip()
    current = Path(raw)
    if not raw:
        return "global"
    aliases = _scope_aliases()
    # Preserve exact folder aliases, including names canonicalize_slug folds
    # and symlink names that resolve to differently named project folders.
    folder_slug = re.sub(r"\s+", "-", current.name.strip().lower())
    if folder_slug in aliases:
        return aliases[folder_slug]
    try:
        current = current.resolve()
    except (OSError, ValueError):
        pass
    if not current.name:
        return "global"
    try:
        roots = _git_project_root(current)
        if roots is None and current.is_dir() and sum(
            1 for child in current.iterdir()
            if child.is_dir() and (child / ".git").exists()
        ) > 1:
            return "global"
    except (OSError, ValueError):
        roots = None
    if roots is not None:
        checkout, root = roots
        checkout_slug = re.sub(r"\s+", "-", checkout.name.strip().lower())
        if checkout_slug in aliases:
            return aliases[checkout_slug]
        name = root.name
    else:
        name = current.name
    slug = canonicalize_slug(name)
    folder_slug = re.sub(r"\s+", "-", name.strip().lower())
    return aliases.get(folder_slug, aliases.get(slug, f"project:{slug}"))


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
