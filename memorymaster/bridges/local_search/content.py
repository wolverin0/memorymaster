"""Bounded, read-only content search for the ``local_search`` MCP tool.

Pipeline (nothing is indexed, cached or written):

1. *Candidates* — file paths from :class:`EverythingProvider` (name, folder and
   extension hints) or, when Everything is unavailable, a pruned directory walk
   of the requested folder/workspace.
2. *Filter* — a candidate is read only if it sits under a registered root
   (:func:`load_roots`, symlinks resolved), is not under an excluded folder, is
   not a credential-looking file, is within the per-file byte cap and is not
   binary.  The Python filters are authoritative; the Everything hints only
   narrow what Everything returns.
3. *Match* — ``rg --json`` over the accepted files when ripgrep is on PATH
   (detected once), otherwise a plain Python line scan with identical
   semantics: literal, case-insensitive substring match per line.
4. *Redact* — every path becomes a root-relative token and every snippet goes
   through :func:`memorymaster.core.security.redact_text` before it leaves.

Configuration (env vars, all optional):
    MEMORYMASTER_LOCAL_SEARCH_CONTENT:            ``0``/``off`` disables content search.
    MEMORYMASTER_LOCAL_SEARCH_EXCLUDE_DIRS:       Extra excluded folders, ``;``-separated.
                                                  A bare name matches any path segment;
                                                  an absolute path excludes that subtree.
                                                  Always added to the built-in defaults.
    MEMORYMASTER_LOCAL_SEARCH_MAX_FILE_BYTES:     Files larger than this are skipped (1 MiB).
    MEMORYMASTER_LOCAL_SEARCH_MAX_RESPONSE_BYTES: Serialized hit budget per call (64 KiB).
    MEMORYMASTER_LOCAL_SEARCH_TIMEOUT:            Total seconds for the whole scan (10).
    MEMORYMASTER_LOCAL_SEARCH_RG_PATH:            Explicit rg binary, or ``none`` to force
                                                  the Python scan.
"""
from __future__ import annotations

import fnmatch
import functools
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Iterable, Iterator
from typing import Any

from memorymaster.bridges.local_search.redact import expand_path, redact_path_for_output

__all__ = [
    "ContentSearchError",
    "HARD_MAX_CANDIDATES",
    "MAX_SNIPPET_CHARS",
    "make_snippet",
    "rg_binary",
    "run_content_search",
]

_LOG = logging.getLogger(__name__)

_ENV_ENABLED = "MEMORYMASTER_LOCAL_SEARCH_CONTENT"
_ENV_EXCLUDE = "MEMORYMASTER_LOCAL_SEARCH_EXCLUDE_DIRS"
_ENV_MAX_FILE = "MEMORYMASTER_LOCAL_SEARCH_MAX_FILE_BYTES"
_ENV_MAX_RESPONSE = "MEMORYMASTER_LOCAL_SEARCH_MAX_RESPONSE_BYTES"
_ENV_TIMEOUT = "MEMORYMASTER_LOCAL_SEARCH_TIMEOUT"
_ENV_RG = "MEMORYMASTER_LOCAL_SEARCH_RG_PATH"

MAX_SNIPPET_CHARS = 200
_SNIPPET_WINDOW = 1000
HARD_MAX_CANDIDATES = 5000
MAX_FILES_SCANNED = 2000
DEFAULT_MAX_HITS = 50
HARD_MAX_HITS = 200
DEFAULT_MAX_PER_FILE = 20
_DEFAULT_FILE_BYTES = 1024 * 1024
_DEFAULT_RESPONSE_BYTES = 64 * 1024
_DEFAULT_TIMEOUT = 10.0
_MAX_QUERY_CHARS = 200
_SNIFF_BYTES = 8192
_RG_BATCH_FILES = 150
_RG_BATCH_CHARS = 20000
_REDACTED_SNIPPET = "[redacted: sensitive content]"

# Folder names skipped anywhere below a root.  Env entries only ever add to it.
DEFAULT_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages",
        # MemoryMaster state and credential-looking folders.
        ".memorymaster", ".ssh", ".aws", ".azure", ".gnupg", ".kube", ".docker",
        ".gemini", ".codex", ".claude", ".config", "appdata",
    }
)

# Credential-looking file names, matched case-insensitively on the basename.
_SENSITIVE_FILE_PATTERNS: tuple[str, ...] = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.pfx", "*.p12", "*.ppk", "*.jks",
    "*.keystore", "*.kdbx", "*.tfstate", "*.token", "id_rsa*", "id_dsa*", "id_ecdsa*",
    "id_ed25519*", ".netrc", "_netrc", ".npmrc", ".pypirc", ".pgpass", ".git-credentials",
    "credentials", "credentials.*", "*secret*", "service-account*.json",
)

_EXT_RE = re.compile(r"^[a-z0-9][a-z0-9_+\-]{0,15}$")


class ContentSearchError(ValueError):
    """Caller-correctable problem; ``field`` names the offending parameter."""

    def __init__(self, message: str, field: str | None = None, code: str = "VALIDATION_ERROR") -> None:
        super().__init__(message)
        self.field = field
        self.code = code


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def _env_number(name: str, default: float, minimum: float, cast: type = float) -> Any:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        _LOG.warning("Invalid %s=%r - using default", name, raw)
        return default
    return value if value >= minimum else default


def content_search_enabled() -> bool:
    return os.environ.get(_ENV_ENABLED, "").strip().lower() not in {"0", "false", "off", "no"}


@functools.lru_cache(maxsize=1)
def rg_binary() -> str | None:
    """Locate ripgrep once per process (``cache_clear()`` to re-detect)."""
    configured = os.environ.get(_ENV_RG, "").strip()
    if configured.lower() in {"none", "off", "0"}:
        return None
    if configured:
        return configured if os.path.isfile(configured) else None
    return shutil.which("rg")


class _Excludes:
    """Excluded folder names (any segment) and absolute subtrees."""

    def __init__(self) -> None:
        self.names: set[str] = {n.lower() for n in DEFAULT_EXCLUDED_DIRS}
        self.prefixes: list[str] = []
        for entry in os.environ.get(_ENV_EXCLUDE, "").split(";"):
            entry = entry.strip()
            if not entry:
                continue
            if os.path.isabs(entry):
                self.prefixes.append(_norm(os.path.realpath(entry)))
            else:
                self.names.add(entry.strip("/\\").lower())

    def dir_excluded(self, name: str, full_path: str) -> bool:
        if name.lower() in self.names:
            return True
        if self.prefixes:
            norm = _norm(full_path)
            return any(_is_within(prefix, norm) for prefix in self.prefixes)
        return False

    def path_excluded(self, full_path: str, rel_dirs: Iterable[str]) -> bool:
        if any(part.lower() in self.names for part in rel_dirs):
            return True
        if self.prefixes:
            norm = _norm(full_path)
            return any(_is_within(prefix, norm) for prefix in self.prefixes)
        return False


def _is_sensitive_name(basename: str) -> bool:
    lowered = basename.lower()
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in _SENSITIVE_FILE_PATTERNS)


# ---------------------------------------------------------------------------
# path helpers
# ---------------------------------------------------------------------------


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _is_within(root_norm: str, path_norm: str) -> bool:
    return path_norm == root_norm or path_norm.startswith(root_norm.rstrip(os.sep) + os.sep)


def _rel_dirs(root_norm: str, path_norm: str) -> list[str]:
    rel = path_norm[len(root_norm):].strip(os.sep)
    parts = [p for p in rel.split(os.sep) if p]
    return parts[:-1]


class _Roots:
    """Registered roots in lexical and symlink-resolved form."""

    def __init__(self, roots: list[tuple[str, str]]) -> None:
        self.lexical = [_norm(path) for _name, path in roots if path]
        self.real = [_norm(os.path.realpath(path)) for _name, path in roots if path]

    def lexical_root(self, path_norm: str) -> str | None:
        best = None
        for root in self.lexical:
            if _is_within(root, path_norm) and (best is None or len(root) > len(best)):
                best = root
        return best

    def real_root(self, path_norm: str) -> str | None:
        best = None
        for root in self.real:
            if _is_within(root, path_norm) and (best is None or len(root) > len(best)):
                best = root
        return best


def _resolve_dir(roots: _Roots, raw_roots: list[tuple[str, str]], value: str, base: str, field: str) -> str:
    expanded = expand_path(raw_roots, value.strip())
    candidate = os.path.realpath(os.path.join(base, expanded))
    if not os.path.isdir(candidate) or roots.real_root(_norm(candidate)) is None:
        raise ContentSearchError(
            f"{field} must be an existing directory under a registered root", field
        )
    return candidate


def parse_extensions(raw: str) -> frozenset[str]:
    out: set[str] = set()
    for item in re.split(r"[;,\s]+", str(raw or "")):
        item = item.strip().lstrip(".").lower()
        if not item:
            continue
        if not _EXT_RE.match(item):
            raise ContentSearchError(
                "extension must be a list of plain extensions such as 'py;md'", "extension"
            )
        out.add(item)
    if len(out) > 20:
        raise ContentSearchError("extension accepts at most 20 entries", "extension")
    return frozenset(out)


# ---------------------------------------------------------------------------
# candidate filtering
# ---------------------------------------------------------------------------


def _open_binary(path: str):
    return open(path, "rb")  # noqa: SIM115 - callers use it as a context manager


def _looks_binary(path: str) -> bool:
    try:
        with _open_binary(path) as fh:
            return b"\x00" in fh.read(_SNIFF_BYTES)
    except OSError:
        return True


class _Filter:
    def __init__(
        self,
        roots: _Roots,
        excludes: _Excludes,
        *,
        extensions: frozenset[str],
        scope_real: str | None,
        max_file_bytes: int,
    ) -> None:
        self.roots = roots
        self.excludes = excludes
        self.extensions = extensions
        self.scope_real = scope_real
        self.max_file_bytes = max_file_bytes

    def accept(self, path: str) -> str | None:
        """Return the lexical path when it may be read, else ``None``."""
        if not path or "\x00" in path:
            return None
        lexical = _norm(path)
        lex_root = self.roots.lexical_root(lexical)
        if lex_root is None:
            return None
        try:
            real = _norm(os.path.realpath(path))
        except (OSError, ValueError):
            return None
        real_root = self.roots.real_root(real)
        if real_root is None:
            return None
        if self.scope_real is not None and not _is_within(self.scope_real, real):
            return None
        if self.excludes.path_excluded(lexical, _rel_dirs(lex_root, lexical)):
            return None
        if self.excludes.path_excluded(real, _rel_dirs(real_root, real)):
            return None
        if _is_sensitive_name(os.path.basename(lexical)) or _is_sensitive_name(os.path.basename(real)):
            return None
        if self.extensions:
            suffix = os.path.splitext(lexical)[1].lstrip(".")
            if suffix not in self.extensions:
                return None
        try:
            if not os.path.isfile(path):
                return None
            size = os.stat(path).st_size
        except OSError:
            return None
        if size <= 0 or size > self.max_file_bytes:
            return None
        if _looks_binary(path):
            return None
        return path


def _name_matcher(name_query: str, whole_name: bool):
    terms = [t.lower() for t in str(name_query or "").split() if t]
    if not terms or terms == ["*"]:
        return lambda _basename: True

    def one(term: str, base: str) -> bool:
        if any(ch in term for ch in "*?["):
            return fnmatch.fnmatchcase(base, term)
        return base == term if whole_name else term in base

    return lambda basename: all(one(t, basename.lower()) for t in terms)


def _walk(scope_dir: str, excludes: _Excludes, matcher, deadline: float) -> Iterator[str]:
    for dirpath, dirnames, filenames in os.walk(scope_dir, followlinks=False):
        if time.monotonic() >= deadline:
            return
        dirnames[:] = sorted(d for d in dirnames if not excludes.dir_excluded(d, os.path.join(dirpath, d)))
        for filename in sorted(filenames):
            if matcher(filename):
                yield os.path.join(dirpath, filename)


# ---------------------------------------------------------------------------
# snippets
# ---------------------------------------------------------------------------


def make_snippet(line: str, query: str) -> str:
    """A redacted snippet of at most :data:`MAX_SNIPPET_CHARS` around the match.

    Redaction runs on the whole bounded window *before* the final cut, so a
    secret that straddles the cut is replaced, never half-shown.
    """
    from memorymaster.core.security import redact_text, scan_text_for_findings

    text = line.rstrip("\r\n").replace("\t", " ")
    needle = query.lower()
    idx = text.lower().find(needle)
    if len(text) > _SNIPPET_WINDOW:
        start = max(0, idx - _SNIPPET_WINDOW // 2) if idx >= 0 else 0
        text = text[start:start + _SNIPPET_WINDOW]
    redacted, _findings = redact_text(text)
    if scan_text_for_findings(redacted):
        return _REDACTED_SNIPPET
    redacted = redacted.strip()
    if len(redacted) <= MAX_SNIPPET_CHARS:
        return redacted
    pos = redacted.lower().find(needle)
    start = max(0, pos - MAX_SNIPPET_CHARS // 3) if pos >= 0 else 0
    return redacted[start:start + MAX_SNIPPET_CHARS].strip()


class _Collector:
    def __init__(self, roots: list[tuple[str, str]], query: str, max_hits: int, max_per_file: int,
                 max_response_bytes: int) -> None:
        self.roots = roots
        self.query = query
        self.max_hits = max_hits
        self.max_per_file = max_per_file
        self.max_response_bytes = max_response_bytes
        self.hits: list[dict[str, Any]] = []
        self.bytes_used = 2
        self.truncated = False
        self._per_file: dict[str, int] = {}

    def add(self, path: str, line_no: int, text: str) -> bool:
        """Record one match; ``False`` means stop scanning (cap reached)."""
        seen = self._per_file.get(path, 0)
        if seen >= self.max_per_file:
            self.truncated = True
            return True  # keep scanning other files
        if len(self.hits) >= self.max_hits:
            self.truncated = True
            return False
        hit = {
            "path": redact_path_for_output(self.roots, path),
            "line": int(line_no),
            "snippet": make_snippet(text, self.query),
        }
        size = len(json.dumps(hit, ensure_ascii=False).encode("utf-8")) + 2
        if self.bytes_used + size > self.max_response_bytes:
            self.truncated = True
            return False
        self.bytes_used += size
        self._per_file[path] = seen + 1
        self.hits.append(hit)
        return True


# ---------------------------------------------------------------------------
# engines
# ---------------------------------------------------------------------------


def _grep_python(paths: list[str], query: str, max_per_file: int, max_file_bytes: int,
                 deadline: float) -> Iterator[tuple[str, int, str]]:
    needle = query.lower()
    for path in paths:
        if time.monotonic() >= deadline:
            return
        try:
            with _open_binary(path) as fh:
                data = fh.read(max_file_bytes + 1)
        except OSError:
            continue
        found = 0
        for number, raw in enumerate(data.split(b"\n"), start=1):
            if number % 4096 == 0 and time.monotonic() >= deadline:
                return
            line = raw.decode("utf-8", errors="replace").rstrip("\r")
            if needle in line.lower():
                yield path, number, line
                found += 1
                if found > max_per_file:
                    break


def _rg_stream(argv: list[str], timeout: float) -> Iterator[bytes]:
    """Run rg and yield stdout lines; the process is killed at *timeout* or on early exit."""
    proc = subprocess.Popen(  # noqa: S603 - argv list, shell never used
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    timer = threading.Timer(max(timeout, 0.01), proc.kill)
    timer.start()
    try:
        assert proc.stdout is not None
        yield from iter(proc.stdout.readline, b"")
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
        if proc.stdout is not None:
            proc.stdout.close()
        proc.wait()


def _json_text(node: Any) -> str | None:
    """rg encodes non-UTF-8 as ``{"bytes": ...}``; those records are skipped."""
    if isinstance(node, dict) and isinstance(node.get("text"), str):
        return node["text"]
    return None


def _grep_rg(rg: str, batch: list[str], query: str, max_per_file: int, max_file_bytes: int,
             deadline: float) -> Iterator[tuple[str, int, str]]:
    argv = [
        rg, "--json", "--no-config", "--no-messages", "--fixed-strings", "--ignore-case",
        "--threads", "1", "--max-filesize", str(max_file_bytes),
        "--max-count", str(max_per_file + 1), "-e", query, "--", *batch,
    ]
    allowed = {_norm(p): p for p in batch}
    remaining = max(deadline - time.monotonic(), 0.01)
    for raw in _rg_stream(argv, remaining):
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "match":
            continue
        data = record.get("data") or {}
        path_text = _json_text(data.get("path"))
        line_text = _json_text(data.get("lines"))
        number = data.get("line_number")
        if path_text is None or line_text is None or not isinstance(number, int):
            continue
        original = allowed.get(_norm(path_text))
        if original is None:
            continue  # rg may only report files we handed it
        yield original, number, line_text


def _batches(paths: Iterable[str]) -> Iterator[list[str]]:
    batch: list[str] = []
    chars = 0
    for path in paths:
        if batch and (len(batch) >= _RG_BATCH_FILES or chars + len(path) + 1 > _RG_BATCH_CHARS):
            yield batch
            batch, chars = [], 0
        batch.append(path)
        chars += len(path) + 1
    if batch:
        yield batch


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def run_content_search(
    provider: Any,
    roots: list[tuple[str, str]],
    *,
    name_query: str,
    content_query: str,
    workspace: str = ".",
    folder: str = "",
    extension: str = "",
    whole_name: bool = False,
    max_hits: int = DEFAULT_MAX_HITS,
    max_per_file: int = DEFAULT_MAX_PER_FILE,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Search file contents; returns already-redacted hits plus run metadata."""
    if not content_search_enabled():
        raise ContentSearchError("content search is disabled", "content_query", "DISABLED")
    query = str(content_query or "").strip()
    if not query:
        raise ContentSearchError("content_query must be a non-empty string", "content_query")
    if len(query) > _MAX_QUERY_CHARS or "\x00" in query or "\n" in query or "\r" in query:
        raise ContentSearchError(
            f"content_query must be a single line of at most {_MAX_QUERY_CHARS} characters", "content_query"
        )
    extensions = parse_extensions(extension)
    max_hits = max(1, min(int(max_hits), HARD_MAX_HITS))
    max_per_file = max(1, min(int(max_per_file), 1000))
    max_file_bytes = int(_env_number(_ENV_MAX_FILE, _DEFAULT_FILE_BYTES, 1, int))
    max_response_bytes = int(_env_number(_ENV_MAX_RESPONSE, _DEFAULT_RESPONSE_BYTES, 256, int))
    total_timeout = float(_env_number(_ENV_TIMEOUT, _DEFAULT_TIMEOUT, 0.0)) if timeout is None else float(timeout)
    deadline = time.monotonic() + max(total_timeout, 0.0)

    root_set = _Roots(roots)
    base = os.path.realpath(workspace or ".")
    # Default scope is the workspace, never "whatever Everything returns under
    # any root"; ``folder`` is the explicit way to search somewhere else.
    if str(folder or "").strip():
        scope_dir = _resolve_dir(root_set, roots, folder, base, "folder")
    else:
        scope_dir = _resolve_dir(root_set, roots, ".", base, "workspace")
    excludes = _Excludes()
    degraded = not provider.available()

    if degraded:
        walk_dir = scope_dir
        matcher = _name_matcher(name_query, whole_name)
        raw_candidates: Iterable[str] = _walk(walk_dir, excludes, matcher, deadline)
    else:
        es_query = str(name_query or "").strip() or "*"
        if extensions and not whole_name:
            es_query += " ext:" + ";".join(sorted(extensions))
        search_kwargs: dict[str, Any] = {
            "limit": HARD_MAX_CANDIDATES, "kind": "file", "whole_name": bool(whole_name),
        }
        if "under" in inspect.signature(provider.search).parameters:
            search_kwargs["under"] = scope_dir
        found = provider.search(es_query, **search_kwargs)
        raw_candidates = [hit.path for hit in found if getattr(hit, "kind", "file") != "dir"]

    flt = _Filter(
        root_set, excludes, extensions=extensions,
        scope_real=_norm(scope_dir), max_file_bytes=max_file_bytes,
    )

    def accepted() -> Iterator[str]:
        count = 0
        for candidate in raw_candidates:
            if time.monotonic() >= deadline or count >= MAX_FILES_SCANNED:
                return
            ok = flt.accept(candidate)
            if ok is not None:
                count += 1
                yield ok

    collector = _Collector(roots, query, max_hits, max_per_file, max_response_bytes)
    rg = rg_binary()
    engine = "rg" if rg else "python"
    files_scanned = 0

    for batch in _batches(accepted()):
        files_scanned += len(batch)
        stream: Iterator[tuple[str, int, str]]
        if rg and engine == "rg":
            stream = _grep_rg(rg, batch, query, max_per_file, max_file_bytes, deadline)
            emitted = False
            try:
                stop = False
                for path, number, text in stream:
                    emitted = True
                    if not collector.add(path, number, text):
                        stop = True
                        break
                if stop:
                    break
                continue
            except OSError as exc:
                _LOG.warning("ripgrep failed (%s); using the Python scan", exc.__class__.__name__)
                if emitted:
                    collector.truncated = True
                    break
                engine = "python"
        stop = False
        for path, number, text in _grep_python(batch, query, max_per_file, max_file_bytes, deadline):
            if not collector.add(path, number, text):
                stop = True
                break
        if stop:
            break
        if time.monotonic() >= deadline:
            break

    timed_out = time.monotonic() >= deadline
    return {
        "hits": collector.hits,
        "degraded": degraded,
        "engine": engine,
        "files_scanned": files_scanned,
        "truncated": collector.truncated,
        "timed_out": timed_out,
    }
