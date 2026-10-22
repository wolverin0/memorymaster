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
    "resolve_transcript_workspace_scope",
    "resolve_Transcript_workspace_scope",
    "resolve_workspace_scope",
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


def _git_project_root(
    current: Path, *, strict: bool = False, aliases: dict[str, str] | None = None,
) -> tuple[Path, Path] | None:
    """Walk once for checkout/owner roots, with mode-specific proof boundaries.

    Strict capture accepts ancestor aliases, bounds discovery, and refuses
    missing or ephemeral workspaces. Hook discovery honors Git ceilings and
    keeps the checkout when metadata is broken. Both use the same reader.
    """
    aliases = aliases or {}
    ceilings = set() if strict else {
        Path(raw).resolve() for raw in os.environ.get("GIT_CEILING_DIRECTORIES", "").split(os.pathsep) if raw
    }
    candidates = (current,) if current in ceilings else (current, *current.parents)
    if strict:
        candidates = candidates[:_REPOSITORY_WALK_LIMIT + 1]
    unresolved_workspace = strict and not current.is_dir()
    for candidate in candidates:
        if candidate != current and candidate in ceilings:
            break
        if strict and _SLUG_NORMALIZER_RE.sub("-", candidate.name.strip().lower()) in aliases:
            return candidate, candidate
        dotgit = candidate / ".git"
        try:
            has_marker = dotgit.exists() if strict else dotgit.is_dir() or dotgit.is_file()
            if has_marker:
                if unresolved_workspace:
                    return None
                owner = _repository_owner(candidate, strict=strict)
                return (candidate, owner) if owner is not None else None
            if strict and candidate == current and current.is_dir():
                repositories = 0
                for index, child in enumerate(current.iterdir()):
                    if index >= 1024:
                        return None
                    repositories += int(child.is_dir() and (child / ".git").exists())
                    if repositories > 1:
                        return None
            if strict and _EPHEMERAL_WORKSPACE.fullmatch(candidate.name):
                unresolved_workspace = True
        except (OSError, ValueError, UnicodeError):
            return None if strict else (candidate, candidate)
    return None


def scope_from_cwd(cwd: str | os.PathLike[str] | None) -> str:
    """Resolve hook/session/MCP scope, retaining checkout and basename fallbacks."""
    return resolve_workspace_scope(cwd, strict=False) or "global"


def resolve_workspace_scope(
    cwd: str | os.PathLike[str] | None, *, strict: bool = False,
    allow_fallback_global: bool = False,
) -> str | None:
    """Resolve one project identity with an explicit metadata proof policy.

    Strict capture/backfill requires a verified repository or operator alias;
    unresolved paths return None for quarantine. Relaxed hooks retain checkout
    scope on broken metadata and basename/global fallbacks outside Git.
    """
    raw = str(cwd or "").strip()
    current = Path(raw)
    if not raw:
        return "global" if not strict or allow_fallback_global else None
    if current.anchor and not current.name:
        return None if strict else "global"
    aliases = _scope_aliases()
    # Preserve exact folder aliases, including names canonicalize_slug folds
    # and symlink names that resolve to differently named project folders.
    folder_slug = re.sub(r"\s+", "-", current.name.strip().lower())
    if not strict and folder_slug in aliases:
        return aliases[folder_slug]
    try:
        current = current.resolve()
    except (OSError, ValueError, RuntimeError) as error:
        if strict:
            return None
        if isinstance(error, RuntimeError):
            raise
        pass
    if not current.name:
        return None if strict else "global"
    try:
        if strict and current.is_file() and current.suffix == ".jsonl":
            return resolve_workspace_scope(
                cwd_from_transcript(current), strict=True, allow_fallback_global=allow_fallback_global,
            )
        roots = _git_project_root(current, strict=strict, aliases=aliases)
        if not strict and roots is None and current.is_dir() and sum(
            1 for child in current.iterdir()
            if child.is_dir() and (child / ".git").exists()
        ) > 1:
            return "global"
    except (OSError, ValueError, RuntimeError) as error:
        if strict:
            return None
        if isinstance(error, RuntimeError):
            raise
        roots = None
    if roots is not None:
        checkout, root = roots
        checkout_slug = (
            _SLUG_NORMALIZER_RE.sub("-", checkout.name.strip().lower()) if strict
            else re.sub(r"\s+", "-", checkout.name.strip().lower())
        )
        if checkout_slug in aliases:
            return aliases[checkout_slug]
        name = root.name
    else:
        if strict:
            return None
        name = current.name
    slug = canonicalize_slug(name)
    folder_slug = (
        _SLUG_NORMALIZER_RE.sub("-", name.strip().lower()) if strict
        else re.sub(r"\s+", "-", name.strip().lower())
    )
    if strict:
        if _EPHEMERAL_WORKSPACE.fullmatch(name) and folder_slug not in aliases:
            return None
        return aliases.get(folder_slug, f"project:{slug}")
    return aliases.get(folder_slug, aliases.get(slug, f"project:{slug}"))


def project_root_from_cwd(cwd: str | os.PathLike[str] | None) -> Path:
    """The Git project root ``scope_from_cwd`` derives the scope from, or the folder itself.

    Disambiguation digests must hash this, never the worktree or subdirectory path.
    """
    current = Path(str(cwd or "").strip() or ".")
    try:
        current = current.resolve()
    except (OSError, ValueError):
        pass
    try:
        roots = _git_project_root(current)
    except (OSError, ValueError):
        roots = None
    return roots[1] if roots is not None else current


def scope_from_cwd_or_user(cwd: str | os.PathLike[str] | None) -> str:
    """``scope_from_cwd`` for hooks and session defaults: the ``global`` sentinel becomes ``user``.

    Hooks run from collection roots, filesystem roots and empty cwds; an implicit
    ``global`` binding there would leak claims into every project.
    """
    scope = scope_from_cwd(cwd) if cwd else "user"
    return "user" if scope == "global" else scope


_SLUG_NORMALIZER_RE = re.compile(r"\s+")
_EPHEMERAL_WORKSPACE = re.compile(r"^(?:_wt|worktrees?|scratch|tmp|temp|pr[-_]?\d+|t[-_]?\d+|card[-_]?\d+)$", re.I)
_REPOSITORY_WALK_LIMIT = 32
_GIT_METADATA_LIMIT = 4096


def _small_git_text(path: Path, *, strict: bool = True) -> str:
    with path.open("r", encoding="utf-8") as handle:
        text = handle.read(_GIT_METADATA_LIMIT + 1) if strict else handle.read()
    if strict and (len(text) > _GIT_METADATA_LIMIT or "\x00" in text):
        raise ValueError("invalid repository metadata")
    return text.strip()


def _repository_owner(root: Path, *, strict: bool = False) -> Path | None:
    """Read gitfile/commondir once; strict mode also proves membership/backlink.

    Gitfile targets are checkout-relative; commondir is gitdir-relative.
    Relaxed mode keeps submodule identity and tolerates bare common dirs.
    """
    marker = root / ".git"
    if marker.is_dir():
        if strict:
            return root
        gitdir = marker
    elif marker.is_file():
        pointer = _small_git_text(marker, strict=strict)
        if not pointer.startswith("gitdir:") or (strict and "\n" in pointer):
            return None if strict else root
        target = pointer[len("gitdir:"):].strip()
        if not target:
            return None if strict else root
        gitdir = (root / target).resolve()
        if not strict and not gitdir.is_dir():
            return root
    else:
        return None
    commondir = gitdir / "commondir"
    if not strict and not commondir.is_file():
        return root
    common_text = _small_git_text(commondir, strict=strict)
    if not common_text or (strict and "\n" in common_text):
        return None if strict else root
    common = (gitdir / common_text).resolve()
    if not strict:
        if not common.is_dir():
            return root
        if common.name.lower() == ".git":
            return common.parent
        return common.with_suffix("") if common.suffix.lower() == ".git" else common
    # A valid linked worktree points into this exact common .git/worktrees dir.
    # Reject arbitrary commondir pointers that could attribute a scratch repo.
    if common.name != ".git" or not common.is_dir() or gitdir.parent != common / "worktrees":
        return None
    backlink = gitdir / "gitdir"
    if backlink.exists() and Path(_small_git_text(backlink)).resolve() != marker.resolve():
        return None
    owner = common.parent
    return owner if owner.is_dir() else None


def resolve_transcript_workspace_scope(
    path_or_cwd: str | os.PathLike[str] | None, *, allow_fallback_global: bool = False,
) -> str | None:
    """Resolve one canonical project identity, or explicitly leave it unresolved.

    Existing operator aliases take precedence over inferred repository names.
    A deleted worktree can resolve only through an explicit alias. A nearby
    parent repository does not prove ownership of a missing card/scratch path.
    Generic callers may retain their separate empty-cwd ``global`` convention.
    """
    return resolve_workspace_scope(path_or_cwd, strict=True, allow_fallback_global=allow_fallback_global)


# Preserve the spelling supplied in the approved review's interface contract.
resolve_Transcript_workspace_scope = resolve_transcript_workspace_scope


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
