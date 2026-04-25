"""Layer-1 regex entity extractor + Layer-2 LLM assist.

See ``artifacts/spec-entity-extraction-at-ingest-2026-04-23.md`` and the
3.2 expansion artifact ``artifacts/entity-new-kinds-2026-04-23.md``.

Layer 1 (``extract_patterns``): stdlib-only regex pass, kinds
    ``file``, ``env-var``, ``service``, ``port``, ``commit``, ``tool``,
    ``package``, ``url_domain``, ``slash_command``, ``claim_id_ref``.

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

# package: Python/Node/JS package names mentioned in UNAMBIGUOUS install or
# import contexts. Loose English verbs (``from``, ``import``, ``require``)
# as plain context triggers were tried in an earlier revision but over-fired
# on natural prose (e.g. ``from the backend``). Context now requires either:
#   (a) a CLI install verb — ``pip install``, ``npm install``, ``poetry add``
#       and siblings; the "line" after the verb is scanned for tokens, OR
#   (b) a line-anchored Python import statement.
_PACKAGE_CLI_RE = re.compile(
    r"\b(?:"
    r"pip[0-9]?\s+install|"
    r"uv\s+(?:pip\s+)?add|uv\s+pip\s+install|"
    r"poetry\s+add|conda\s+install|pipx\s+install|"
    r"npm\s+install|npm\s+i|pnpm\s+(?:add|i)|"
    r"yarn\s+add|bun\s+(?:add|install)"
    r")\b",
    re.IGNORECASE,
)
# Python import statements, anchored at start-of-line / after ``\n`` so
# prose like ``reverts from...`` or ``import paths are bad`` doesn't fire.
_PACKAGE_PY_IMPORT_RE = re.compile(
    r"(?m)^\s*(?:import\s+([a-z][a-z0-9_]*(?:\s*,\s*[a-z][a-z0-9_]*)*)"
    r"|from\s+([a-z][a-z0-9_.]*)\s+import\s+)",
)
# A single package token: lowercase start, may contain digits, hyphens, dots,
# underscores, but must begin with a letter and be at least 2 chars long.
_PACKAGE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])([a-z][a-z0-9]*(?:[._-][a-z0-9]+)*)(?![A-Za-z0-9_./-])"
)
# Generous English + keyword blocklist — even under tight CLI context the
# user may write ``pip install --upgrade with the cache cleared``.
_PACKAGE_BLOCKLIST = frozenset(
    {
        # CLI flags / keywords
        "as", "from", "import", "install", "add", "require", "requires",
        "dev", "devdependencies", "save", "global", "save-dev", "upgrade",
        # Common python / shell noise
        "os", "sys", "re", "io", "time", "typing",
        "sudo", "bash", "sh", "exec", "cd",
        # Generic English stopwords.
        "the", "and", "or", "but", "for", "with", "without", "into",
        "this", "that", "these", "those", "some", "any", "all",
        "will", "would", "can", "could", "may", "might", "should",
        "need", "needs", "needed", "want", "wants", "wanted",
        "also", "only", "still", "already", "just", "now", "then",
        "uses", "used", "using", "use", "make", "made", "makes",
        "allow", "allows", "allowed", "block", "blocks", "blocked",
        "true", "false", "none", "null", "yes", "no",
    }
)

# url_domain: http(s) URL host only. Port + path are discarded, host is
# lowercased and the leading ``www.`` is stripped so ``www.GitHub.com`` and
# ``github.com`` canonicalize to the same entity.
_URL_RE = re.compile(
    r"\bhttps?://([A-Za-z0-9][A-Za-z0-9.\-]+\.[A-Za-z]{2,})(?::\d+)?(?:/[^\s]*)?",
    re.IGNORECASE,
)

# slash_command: leading ``/``, lowercase name, optional ``:namespace`` segment.
# Filter: the match must NOT be preceded by an alphanumeric char (so
# ``http://x`` doesn't fire) and must NOT be followed by another ``/segment``
# with word characters (so POSIX paths like ``/usr/bin/foo`` are excluded —
# they have 2+ path segments). ``/wiki`` and ``/superpowers:brainstorming``
# are accepted; ``/tmp/foo`` and ``/var/log/app.log`` are rejected.
_SLASH_COMMAND_RE = re.compile(
    r"(?<![A-Za-z0-9_:/.])(/[a-z][a-z0-9_:-]{1,})(?![a-zA-Z0-9_:])"
)

# claim_id_ref: references to MemoryMaster claims by numeric id or hashed
# ``mm-`` prefix. Accepts ``claim 11822``, ``claims 11822, 11823``, and
# ``mm-abcd1234`` / ``mm-abcd1234~0``. We deliberately do NOT match bare
# 4-6 digit numbers — only when the ``claim`` keyword precedes them.
_CLAIM_NUM_RE = re.compile(r"\bclaims?\s+(\d{4,6})\b", re.IGNORECASE)
_CLAIM_MM_RE = re.compile(r"\b(mm-[a-f0-9]{4,}(?:~[0-9]+)?)\b", re.IGNORECASE)


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


def _canonical_package(surface: str) -> str:
    """Lowercase and normalize ``_``/``.`` separators to ``-`` for packages.

    PyPI treats ``scikit_learn``, ``scikit.learn`` and ``scikit-learn`` as the
    same project; npm is effectively case-insensitive. Normalizing keeps the
    alias graph from fragmenting.
    """
    s = surface.strip().lower()
    # PyPI canonical-name rule: PEP 503. Collapse runs of [-_.] to a single '-'.
    return re.sub(r"[-_.]+", "-", s).strip("-")


def _canonical_url_domain(host_surface: str) -> str:
    s = host_surface.strip().lower().rstrip(".")
    if s.startswith("www."):
        s = s[4:]
    return s


def _canonical_slash_command(surface: str) -> str:
    return surface.strip().lower()


def _canonical_claim_numeric(num: str) -> str:
    return f"claim_{num.strip()}"


def _canonical_claim_mm(surface: str) -> str:
    return surface.strip().lower()


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


def _is_plausible_package(token: str) -> bool:
    """Reject packages that are really English words, flags, or too short."""
    t = token.strip().lower()
    if len(t) < 2 or len(t) > 60:
        return False
    if t in _PACKAGE_BLOCKLIST:
        return False
    if not t[0].isalpha():
        return False
    # Require either a hyphen/underscore/dot OR at least 4 chars — avoids
    # over-matching on short English filler like "for", "but", "the".
    if len(t) < 4 and not re.search(r"[-_.]", t):
        return False
    return True


def _iter_package_mentions(text: str) -> list[tuple[str, str]]:
    """Yield ``(surface, canonical)`` pairs for package-like tokens.

    Two context modes:

    1. CLI install verb — take the IMMEDIATE contiguous run of package-like
       tokens that follows. As soon as a non-package token appears (English
       word, punctuation, shell operator), the run stops. This is strict
       on purpose: ``npm install`` inside prose like ``we had to npm
       install for the deploy`` only captures ``for`` which is blocked, so
       nothing lands. ``pip install fastmcp qdrant-client`` captures both.
    2. Python ``import`` / ``from X import`` — parse the specific module
       name captured by ``_PACKAGE_PY_IMPORT_RE``.
    """
    out: list[tuple[str, str]] = []

    # Mode 1 — CLI install invocations. Strict contiguous run, max 5 tokens.
    for verb in _PACKAGE_CLI_RE.finditer(text):
        start = verb.end()
        newline_pos = text.find("\n", start)
        window_end = newline_pos if newline_pos != -1 else len(text)
        comment_pos = text.find("#", start, window_end)
        if comment_pos != -1:
            window_end = comment_pos
        # Stop at backtick/quote — those bracket code snippets in prose.
        for stop_ch in ("`", "'", '"'):
            stop_pos = text.find(stop_ch, start, window_end)
            if stop_pos != -1:
                window_end = min(window_end, stop_pos)
        window = text[start:window_end]

        # Walk forward one token at a time. A token must be IMMEDIATELY
        # adjacent (separated only by spaces / tabs) to the previous one, or
        # to the verb for the first token. Any gap that contains a non-space
        # character breaks the run.
        cursor = 0
        grabbed = 0
        while grabbed < 5 and cursor < len(window):
            # Consume leading whitespace.
            gap_match = re.match(r"[\s]*", window[cursor:])
            if gap_match:
                cursor += gap_match.end()
            if cursor >= len(window):
                break
            # Skip CLI flags like ``--upgrade`` or ``-r``.
            if window[cursor] == "-":
                flag_match = re.match(r"-+[A-Za-z0-9][A-Za-z0-9_-]*", window[cursor:])
                if flag_match:
                    cursor += flag_match.end()
                    continue
                break
            tok_match = _PACKAGE_TOKEN_RE.match(window[cursor:])
            if not tok_match:
                break
            tok = tok_match.group(1)
            if not _is_plausible_package(tok):
                # Non-package token terminates the run.
                break
            canonical = _canonical_package(tok)
            if canonical:
                out.append((tok, canonical))
                grabbed += 1
            cursor += tok_match.end()

    # Mode 2 — Python imports (line-anchored).
    for m in _PACKAGE_PY_IMPORT_RE.finditer(text):
        captures = [g for g in m.groups() if g]
        for capture in captures:
            for raw in re.split(r"\s*,\s*", capture):
                head = raw.split()[0] if raw.split() else raw
                # ``from pkg.sub import X`` — top-level package only.
                tok = head.split(".")[0].strip().lower()
                if not _is_plausible_package(tok):
                    continue
                canonical = _canonical_package(tok)
                if canonical:
                    out.append((tok, canonical))
    return out


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

    # package (3.2) — context-aware: ``pip install foo``, ``import bar``.
    for surface, canonical in _iter_package_mentions(text):
        _add(surface, "package", canonical)

    # url_domain (3.2) — extract host from http(s) URLs.
    for m in _URL_RE.finditer(text):
        host = m.group(1)
        canonical = _canonical_url_domain(host)
        if not canonical or "." not in canonical:
            continue
        _add(host, "url_domain", canonical)

    # slash_command (3.2) — Claude Code / skill invocations like ``/wiki``.
    for m in _SLASH_COMMAND_RE.finditer(text):
        surface = m.group(1)
        # Reject POSIX paths: if the match is immediately followed by
        # ``/<word>`` in the source text, it's a path, not a command.
        tail = text[m.end():m.end() + 2]
        if tail.startswith("/") and len(tail) > 1 and (tail[1].isalnum() or tail[1] == "."):
            continue
        canonical = _canonical_slash_command(surface)
        _add(surface, "slash_command", canonical)

    # claim_id_ref (3.2) — ``claim 11822`` and ``mm-<hex>`` forms.
    for m in _CLAIM_NUM_RE.finditer(text):
        num = m.group(1)
        _add(m.group(0), "claim_id_ref", _canonical_claim_numeric(num))
    for m in _CLAIM_MM_RE.finditer(text):
        surface = m.group(1)
        _add(surface, "claim_id_ref", _canonical_claim_mm(surface))

    return found


# -- Layer 2 — LLM assist ---------------------------------------------------

# Version identifier baked into the prompt. Bump this string when the prompt
# changes so that downstream idempotency / caching keys invalidate cleanly.
LLM_PROMPT_VERSION = "entity-l2-v2-2026-04-25"

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

_LLM_PROMPT = f"""Extract entities from the snippet that regex cannot catch.
Prompt version: {LLM_PROMPT_VERSION}

Allowed kinds: person_name, spanish_surname, time_expression, model_name, library_name, concept.
Skip: file paths, env-vars, hostnames, ports, commit SHAs, tool names.
Max {_LLM_MAX_ENTITIES} entities. Output STRICT JSON ARRAY only — no prose, no code fence.

Schema (use EXACT field names):
  [{{"kind": "...", "surface_form": "exact substring from text", "aliases": []}}]

Example input: "Ada Lovelace y Charles Babbage usaron FastAPI y gpt-4o-mini."
Example output: [{{"kind":"person_name","surface_form":"Ada Lovelace","aliases":[]}},{{"kind":"person_name","surface_form":"Charles Babbage","aliases":[]}},{{"kind":"library_name","surface_form":"FastAPI","aliases":[]}},{{"kind":"model_name","surface_form":"gpt-4o-mini","aliases":[]}}]

If nothing fits, return: []
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
        # Accept both "surface_form" (standard) and "entity" (Gemma variant)
        surface = (
            str(row.get("surface_form") or row.get("entity") or row.get("text") or "").strip()
        )
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
            aliases = []
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
