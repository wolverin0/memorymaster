"""Layer-1 regex entity extractor. See spec-entity-extraction-at-ingest-2026-04-23.md.

Kinds: file, env-var, service, port, commit, tool (stdlib-only).
Public API: extract_patterns(text) -> list[Entity], deduped by (kind, canonical_hint).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Entity", "extract_patterns", "TOOL_ALLOWLIST"]


@dataclass(frozen=True)
class Entity:
    surface: str
    kind: str
    canonical_hint: str


# -- Patterns ---------------------------------------------------------------

# file: multi-segment paths, single leaves `name.ext`, bare dirs `name/`.
_FILE_MULTI_RE = re.compile(
    r"(?<![A-Za-z0-9_\-])(/?[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+/?)(?![A-Za-z0-9])"
)
_FILE_LEAF_RE = re.compile(
    r"(?<![A-Za-z0-9_/\-.])([A-Za-z_][A-Za-z0-9_\-]*\.[A-Za-z][A-Za-z0-9]{0,5})(?![A-Za-z0-9/])"
)
_FILE_DIR_RE = re.compile(
    r"(?<![A-Za-z0-9_/\-])([a-z_][a-z0-9_\-]{2,}/)(?![A-Za-z0-9])"
)
_FILE_LEAF_EXTENSIONS = frozenset(
    {
        "py", "pyi", "js", "jsx", "ts", "tsx", "json", "jsonl", "yaml",
        "yml", "toml", "md", "mdx", "sql", "sh", "bash", "zsh", "ps1",
        "rs", "go", "rb", "php", "java", "kt", "swift", "c", "cpp", "h",
        "hpp", "html", "css", "scss", "env", "ini", "conf", "txt",
        "log", "csv", "xml", "vue", "svelte", "base",
    }
)

# env-var: ALL_CAPS with at least one underscore.
_ENV_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)(?![A-Za-z0-9_])")

# service/hostname: lowercase, 2+ dash-separated segments.
_SERVICE_RE = re.compile(
    r"(?<![A-Za-z0-9_\-/])([a-z][a-z0-9]+(?:-[a-z0-9]+){1,})(?![A-Za-z0-9_\-/])"
)

# port: ":NNNN" or "port NNNN" (timestamps like 02:00 rejected via lookbehind).
_PORT_COLON_RE = re.compile(r"(?<![0-9:])(:\d{1,5})\b")
_PORT_WORD_RE = re.compile(r"\b(port\s+\d{1,5})\b", re.IGNORECASE)

# commit: 7-40 hex chars. Short forms (7-8) require commit-ish context word.
_HEX_RE = re.compile(r"(?<![A-Za-z0-9])([a-f0-9]{7,40})(?![A-Za-z0-9])")
_COMMIT_CONTEXT_RE = re.compile(
    r"\b(commit|sha|rebase|cherry[- ]pick|hotfix|revert|rollback|merge|"
    r"introduced at|fix(?:ed)? in|shipped in|target|reverted)\b",
    re.IGNORECASE,
)

# tool: allowlist + mcp__<server>__<tool> MCP ids.
TOOL_ALLOWLIST: tuple[str, ...] = (
    "playwright",
    "codex",
    "claude-code",
    "gemini",
    "ollama",
    "droid",
    "opencode",
    "gitnexus",
    "serena",
    "graphify",
    "pytest",
    "ruff",
    "mypy",
    "pyright",
    "ripgrep",
    "docker",
    "git",
    "npm",
)
_TOOL_WORD_RE = re.compile(
    r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(t) for t in TOOL_ALLOWLIST) + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_MCP_TOOL_RE = re.compile(r"\bmcp__[a-zA-Z0-9_\-]+__[a-zA-Z0-9_\-]+\b")


# -- Helpers ----------------------------------------------------------------


def _canonical_file(surface: str) -> str:
    return surface.strip().rstrip("/").rstrip(".,;:!?").lower()


def _canonical_env(surface: str) -> str:
    return surface.strip()


def _canonical_service(surface: str) -> str:
    return surface.strip().lower()


def _canonical_port(surface: str) -> str:
    m = re.search(r"\d+", surface)
    return f":{m.group(0)}" if m else surface.strip()


def _canonical_commit(surface: str) -> str:
    return surface.strip().lower()


def _canonical_tool(surface: str) -> str:
    s = surface.strip()
    return s if s.startswith("mcp__") else s.lower()


# English-phrase blocklist so multi-dash noise doesn't pass as a service.
_SERVICE_BLOCKLIST = frozenset(
    {
        "up-to-date",
        "end-to-end",
        "out-of-band",
        "one-to-one",
        "one-to-many",
        "many-to-many",
        "on-the-fly",
        "state-of-the-art",
        "self-signed-cert",
    }
)


def _is_plausible_service(surface: str) -> bool:
    s = surface.lower()
    if s in _SERVICE_BLOCKLIST:
        return False
    segments = s.split("-")
    if len(segments) == 2 and len(segments[0]) < 7:
        return False
    return any(len(seg) >= 3 for seg in segments)


def _is_plausible_commit(surface: str, full_text: str) -> bool:
    if len(surface) >= 9:
        return True
    return bool(_COMMIT_CONTEXT_RE.search(full_text))


def _is_plausible_port(num: int) -> bool:
    return num in {80, 443, 21, 22, 25, 53} or 1024 <= num <= 65535


# -- Public API -------------------------------------------------------------


def extract_patterns(text: str) -> list[Entity]:
    """Extract Entity records deduped by (kind, canonical_hint)."""
    if not text:
        return []

    found: list[Entity] = []
    seen: set[tuple[str, str]] = set()

    def _add(surface: str, kind: str, canonical_hint: str) -> None:
        key = (kind, canonical_hint)
        if key in seen or not canonical_hint:
            return
        seen.add(key)
        found.append(Entity(surface=surface, kind=kind, canonical_hint=canonical_hint))

    for m in _ENV_RE.finditer(text):
        _add(m.group(1), "env-var", _canonical_env(m.group(1)))

    for m in _MCP_TOOL_RE.finditer(text):
        _add(m.group(0), "tool", _canonical_tool(m.group(0)))

    for m in _FILE_MULTI_RE.finditer(text):
        surface = m.group(1).rstrip(".,;:!?")
        if surface.startswith("mcp__") or "/" not in surface:
            continue
        _add(surface, "file", _canonical_file(surface))

    for m in _FILE_LEAF_RE.finditer(text):
        surface = m.group(1)
        if surface.rsplit(".", 1)[-1].lower() not in _FILE_LEAF_EXTENSIONS:
            continue
        _add(surface, "file", _canonical_file(surface))

    for m in _FILE_DIR_RE.finditer(text):
        surface = m.group(1)
        canonical = surface.rstrip("/").lower()
        if canonical in {"and", "the", "for", "per", "via", "this", "that"}:
            continue
        _add(surface, "file", canonical)

    already_files = {e.canonical_hint for e in found if e.kind == "file"}
    for m in _SERVICE_RE.finditer(text):
        surface = m.group(1)
        if "/" in surface or surface in already_files:
            continue
        if not _is_plausible_service(surface):
            continue
        _add(surface, "service", _canonical_service(surface))

    for port_re in (_PORT_COLON_RE, _PORT_WORD_RE):
        for m in port_re.finditer(text):
            surface = m.group(1)
            num_match = re.search(r"\d+", surface)
            if not num_match or not _is_plausible_port(int(num_match.group(0))):
                continue
            _add(surface, "port", _canonical_port(surface))

    for m in _HEX_RE.finditer(text):
        surface = m.group(1)
        if _is_plausible_commit(surface, text):
            _add(surface, "commit", _canonical_commit(surface))

    # Tool allowlist — skip matches that are substrings of already-extracted
    # env-vars/services/files (e.g. GEMINI inside GEMINI_API_KEY, git inside
    # git-server-02).
    other_surfaces = [
        e.canonical_hint for e in found
        if e.kind in ("env-var", "service", "file")
    ]
    for m in _TOOL_WORD_RE.finditer(text):
        surface = m.group(1)
        low = surface.lower()
        if any(low in o.lower() and o.lower() != low for o in other_surfaces):
            continue
        _add(surface, "tool", _canonical_tool(surface))

    return found
