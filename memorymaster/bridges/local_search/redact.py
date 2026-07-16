"""Path redaction helpers for local-search bridges.

Converts absolute filesystem paths to/from root-relative tokens so that
usernames and internal directory structures are never stored verbatim in
claims.  See LOCALFS-SPEC.md §4 (option B) for the design rationale.

Public API
----------
load_roots() -> list[tuple[str, str]]
    Parse MEMORYMASTER_PATH_ROOTS env var plus auto roots.
    Returns sorted longest-prefix-first list of (name, abspath) pairs.

collapse_path(roots, abspath) -> str
    Replace the longest matching root prefix with ``<name>/rel/sub`` token.
    Returns *abspath* unchanged when no root matches (caller must then apply
    the scan guard before ingesting).

expand_path(roots, token) -> str
    Inverse of collapse_path.  Replaces ``<name>/`` prefix with the real
    absolute path.  Returns *token* unchanged when no root name is recognised.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

__all__ = [
    "load_roots",
    "collapse_path",
    "redact_path_for_output",
    "expand_path",
]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ENV_VAR = "MEMORYMASTER_PATH_ROOTS"
_IS_WINDOWS = sys.platform == "win32"


def _normalise(path: str) -> str:
    """Return a normalised absolute path string for prefix comparison."""
    # Resolve separators; do NOT call resolve() — we want the env-declared
    # path, not the OS-resolved symlink target.
    normalised = str(Path(path))
    # Ensure trailing sep so prefix matching cannot confuse /foo with /foobar.
    if not normalised.endswith(os.sep):
        normalised += os.sep
    return normalised


def _prefix_match(root_path_with_sep: str, abspath_with_sep: str) -> bool:
    """Case-insensitive on Windows, case-sensitive elsewhere."""
    if _IS_WINDOWS:
        return abspath_with_sep.lower().startswith(root_path_with_sep.lower())
    return abspath_with_sep.startswith(root_path_with_sep)


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def load_roots() -> list[tuple[str, str]]:
    """Parse root definitions and return (name, abspath) pairs sorted longest-first.

    Sources (merged, deduped, longest-first):
    1. ``MEMORYMASTER_PATH_ROOTS`` env var — semicolon-separated ``name=path`` entries.
    2. Auto-root: parent directory of the current working directory (``workspace``).
    3. Auto-root: ``USERPROFILE`` (Windows) or ``HOME`` (POSIX), labelled ``home``.

    Duplicate paths (normalised) are deduplicated; first name wins.
    """
    roots: list[tuple[str, str]] = []
    seen_paths: set[str] = set()

    # 1. Explicit env var entries
    raw = os.environ.get(_ENV_VAR, "").strip()
    if raw:
        for entry in raw.split(";"):
            entry = entry.strip()
            if not entry:
                continue
            if "=" not in entry:
                continue
            name, _, path = entry.partition("=")
            name = name.strip()
            path = path.strip()
            if not name or not path:
                continue
            normalised = _normalise(path)
            if normalised not in seen_paths:
                seen_paths.add(normalised)
                roots.append((name, path))

    # 2. Auto-root: workspace parent (parent of cwd)
    try:
        workspace_parent = str(Path(os.getcwd()).parent)
        normalised = _normalise(workspace_parent)
        if normalised not in seen_paths:
            seen_paths.add(normalised)
            roots.append(("workspace", workspace_parent))
    except OSError:
        pass

    # 3. Auto-root: user home directory
    home_env = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
    if home_env:
        normalised = _normalise(home_env)
        if normalised not in seen_paths:
            seen_paths.add(normalised)
            roots.append(("home", home_env))

    # Sort longest absolute path first so the most-specific root wins.
    roots.sort(key=lambda pair: len(pair[1]), reverse=True)
    return roots


def collapse_path(roots: list[tuple[str, str]], abspath: str) -> str:
    """Replace the longest matching root prefix with a ``<name>/rel`` token.

    Parameters
    ----------
    roots:
        Ordered list from :func:`load_roots` (longest-prefix-first).
    abspath:
        Absolute filesystem path to collapse.

    Returns
    -------
    str
        Root-relative token like ``projects/memorymaster`` when a root
        matches, or *abspath* **unchanged** when nothing matches.
    """
    abspath_norm = _normalise(abspath)

    for name, root_path in roots:
        root_norm = _normalise(root_path)
        if _prefix_match(root_norm, abspath_norm):
            # Strip the root prefix (and its trailing sep) from the abspath.
            rel = abspath_norm[len(root_norm):]
            # Normalise to forward slashes and strip any trailing sep.
            rel = rel.replace(os.sep, "/").rstrip("/")
            if rel:
                return f"{name}/{rel}"
            # abspath IS the root itself.
            return name
    return abspath


def redact_path_for_output(roots: list[tuple[str, str]], abspath: str) -> str:
    """Return a stable display token without exposing an unregistered parent path."""
    collapsed = collapse_path(roots, abspath)
    if collapsed != abspath:
        return collapsed

    normalised = str(Path(abspath))
    digest = hashlib.sha256(normalised.encode("utf-8", errors="replace")).hexdigest()[:12]
    basename = Path(normalised).name or "root"
    return f"unregistered/{digest}/{basename}"


def expand_path(roots: list[tuple[str, str]], token: str) -> str:
    """Inverse of :func:`collapse_path` — expand a root-relative token.

    Parameters
    ----------
    roots:
        Ordered list from :func:`load_roots`.
    token:
        Token produced by :func:`collapse_path`, e.g. ``projects/memorymaster``.

    Returns
    -------
    str
        Absolute filesystem path when the token's root name is known,
        or *token* unchanged when the prefix does not match any known root.
    """
    for name, root_path in roots:
        prefix = name + "/"
        if token == name:
            return root_path
        if token.startswith(prefix):
            rel = token[len(prefix):]
            # Re-join using the OS separator.
            return str(Path(root_path) / rel.replace("/", os.sep))
    return token
