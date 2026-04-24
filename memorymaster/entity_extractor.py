"""Layer-1 regex entity extractor + Layer-2 LLM assist.

See ``artifacts/spec-entity-extraction-at-ingest-2026-04-23.md``.

Layer 1 (``extract_patterns``): stdlib-only regex pass, kinds
    ``file``, ``env-var``, ``service``, ``port``, ``commit``, ``tool``.

Layer 2 (``extract_llm``): optional LLM pass, gated by env var
    ``MEMORYMASTER_ENTITY_LLM``. Permitted kinds:
    ``person_name``, ``spanish_surname``, ``time_expression``,
    ``model_name``, ``library_name``, ``concept``.

Both return ``list[Entity]`` with the same ``(surface, kind, canonical_hint)``
shape; callers can merge results and dedup on ``(kind, canonical_hint)``.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

__all__ = [
    "Entity",
    "extract_patterns",
    "extract_llm",
    "merge_entities",
    "TOOL_ALLOWLIST",
    "LLM_KINDS",
    "LLM_PROMPT_VERSION",
]

logger = logging.getLogger(__name__)


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


# -- Layer 2 — LLM assist ---------------------------------------------------

# Version identifier baked into the prompt. Bump this string when the prompt
# changes so that downstream idempotency / caching keys invalidate cleanly.
LLM_PROMPT_VERSION = "entity-l2-v1-2026-04-23"

# Permitted entity kinds for Layer-2. Any `kind` returned by the LLM that is
# not in this set is dropped to keep the registry schema predictable.
LLM_KINDS: frozenset[str] = frozenset(
    {
        "person_name",
        "spanish_surname",
        "time_expression",
        "model_name",
        "library_name",
        "concept",
    }
)

_LLM_ENV_FLAG = "MEMORYMASTER_ENTITY_LLM"
_LLM_MAX_TEXT_CHARS = 4000  # Truncate long claims before sending to LLM.
_LLM_MAX_ENTITIES = 8       # Hard cap to keep cost bounded per claim.

_LLM_PROMPT = f"""You are an entity-extraction assistant for a memory database.
Prompt version: {LLM_PROMPT_VERSION}

Given a short text snippet, return a JSON ARRAY of entities that a regex
pattern-matcher cannot reliably catch. Only return entities of these kinds:

  - person_name        (human given + family name, e.g. "Ada Lovelace")
  - spanish_surname    (Spanish-language surname alone, e.g. "Colombero")
  - time_expression    (natural-language time, e.g. "Thursday", "last week")
  - model_name         (AI model names, e.g. "gpt-4o", "claude-3.5-sonnet")
  - library_name       (SDKs/libs, e.g. "FastAPI", "Qdrant", "LangChain")
  - concept            (abstract named concept, e.g. "bitemporal modeling")

Return at most {_LLM_MAX_ENTITIES} entities. DO NOT return file paths,
env-vars, hostnames, ports, commit SHAs, or tool names — those are handled
by the regex layer.

Output format (strict JSON, no prose, no code fence):

  [
    {{"kind": "...", "surface_form": "...", "aliases": ["...", "..."]}}
  ]

Where:
  - kind          one of the six kinds above
  - surface_form  the exact substring you saw in the text
  - aliases       0-3 alternative canonical forms (may be empty)

If nothing fits, return [].
""".strip()


def _canonical_llm(kind: str, surface: str) -> str:
    """Normalize a Layer-2 surface into a stable canonical_hint."""
    s = surface.strip()
    if not s:
        return ""
    if kind in {"model_name", "library_name"}:
        return s.lower()
    if kind == "time_expression":
        return s.lower()
    if kind in {"person_name", "spanish_surname"}:
        # Preserve casing but collapse whitespace.
        return re.sub(r"\s+", " ", s)
    # concept: lowercase + collapse whitespace
    return re.sub(r"\s+", " ", s).lower()


def _parse_llm_payload(raw: str) -> list[dict]:
    """Defensive wrapper around parse_json_response — returns []  on failure."""
    from memorymaster.llm_provider import parse_json_response

    try:
        parsed = parse_json_response(raw)
    except Exception:  # pragma: no cover - parse_json_response already defensive
        logger.warning("entity_l2: parse_json_response raised unexpectedly")
        return []
    if not isinstance(parsed, list):
        return []
    return [row for row in parsed if isinstance(row, dict)]


def _llm_enabled() -> bool:
    raw = os.environ.get(_LLM_ENV_FLAG, "").strip().lower()
    return raw not in {"", "0", "false", "no", "off"}


def extract_llm(text: str, *, provider: str | None = None) -> list[Entity]:
    """Layer-2 LLM-assisted entity extraction.

    Gated by the ``MEMORYMASTER_ENTITY_LLM`` env var. When unset or falsy,
    this is a no-op that returns ``[]``. When enabled, calls the configured
    LLM provider (see ``memorymaster.llm_provider.call_llm``) and parses
    a JSON array of ``{kind, surface_form, aliases}`` objects.

    Idempotent for a given ``(text, LLM_PROMPT_VERSION, model_version)``
    tuple — the prompt sets ``temperature=0.1`` on the provider side. The
    caller is responsible for caching / deduping across runs.

    Defensive: any failure (missing provider, HTTP error, malformed JSON,
    bad kind, empty surface) is logged at WARNING and yields ``[]`` —
    never raises.

    Parameters
    ----------
    text
        Claim body to scan. Truncated to ``_LLM_MAX_TEXT_CHARS`` before send.
    provider
        Optional provider override (forwarded via the
        ``MEMORYMASTER_LLM_PROVIDER`` env var for the duration of the call).
        When ``None``, uses whatever is already configured in the environment.
    """
    if not text or not text.strip():
        return []
    if not _llm_enabled():
        return []

    # Truncate defensively — the LLM has its own token limits, but we also
    # want to bound per-claim cost.
    snippet = text if len(text) <= _LLM_MAX_TEXT_CHARS else text[:_LLM_MAX_TEXT_CHARS]

    previous_provider = os.environ.get("MEMORYMASTER_LLM_PROVIDER")
    if provider:
        os.environ["MEMORYMASTER_LLM_PROVIDER"] = provider
    try:
        from memorymaster.llm_provider import call_llm

        try:
            raw = call_llm(_LLM_PROMPT, snippet)
        except Exception as exc:  # noqa: BLE001 — defensive, never re-raise
            logger.warning("entity_l2: call_llm failed: %s", exc)
            return []
    finally:
        if provider:
            if previous_provider is None:
                os.environ.pop("MEMORYMASTER_LLM_PROVIDER", None)
            else:
                os.environ["MEMORYMASTER_LLM_PROVIDER"] = previous_provider

    if not raw or not raw.strip():
        return []

    rows = _parse_llm_payload(raw)
    if not rows:
        logger.warning(
            "entity_l2: LLM returned no parseable entities (len=%d)", len(raw)
        )
        return []

    found: list[Entity] = []
    seen: set[tuple[str, str]] = set()
    for row in rows[:_LLM_MAX_ENTITIES]:
        kind = str(row.get("kind", "")).strip().lower().replace(" ", "_")
        surface = str(row.get("surface_form", "")).strip()
        if kind not in LLM_KINDS or not surface:
            continue
        canonical = _canonical_llm(kind, surface)
        if not canonical:
            continue
        key = (kind, canonical)
        if key in seen:
            continue
        seen.add(key)
        found.append(Entity(surface=surface, kind=kind, canonical_hint=canonical))

        # Per-row alias suggestions become separate Entity records with the
        # same (kind, canonical_hint) when distinct — downstream
        # `resolve_or_create` + `add_alias` is responsible for the alias
        # table write. We intentionally do NOT fold them into a single
        # Entity because the dataclass is flat.
        aliases = row.get("aliases") or []
        if not isinstance(aliases, list):
            continue
        for alias in aliases[:3]:
            if not isinstance(alias, str):
                continue
            alias = alias.strip()
            if not alias or alias == surface:
                continue
            alias_canonical = _canonical_llm(kind, alias)
            if not alias_canonical:
                continue
            alias_key = (kind, alias_canonical)
            if alias_key in seen:
                continue
            seen.add(alias_key)
            found.append(
                Entity(surface=alias, kind=kind, canonical_hint=alias_canonical)
            )

    return found


def merge_entities(*groups: list[Entity]) -> list[Entity]:
    """Merge multiple Entity lists, deduping on (kind, canonical_hint)."""
    seen: set[tuple[str, str]] = set()
    out: list[Entity] = []
    for group in groups:
        for ent in group:
            key = (ent.kind, ent.canonical_hint)
            if key in seen:
                continue
            seen.add(key)
            out.append(ent)
    return out
