"""Recall tokenizer — extract salient FTS5 tokens from a raw prompt.

Real prompts are 50-500 chars of mixed Spanish/English. FTS5 AND-joins
every token, so full-prompt queries almost never hit. Fix: extract up to
``max_tokens`` salient tokens (stopword filter + IDF + entity-alias boost).
Stdlib only, read-only DB, per-process LRU cache.

See ``artifacts/retrieval-eval-2026-04-22.md`` for the original audit and
``artifacts/recall-zero-hit-prompts-2026-04-23.md`` for the v2 diagnosis.

v2 changes (Wave 1-E, 2026-04-23)
---------------------------------
1. **df=0 demotion.** Earlier pure-IDF ranking preferred unmatchable
   tokens (typos, rants) because ``log((N+1)/1)+1`` peaks when the
   token doesn't exist in the corpus. v2 applies a ``-_DF_ZERO_PENALTY``
   unless the token is also a technical-term allowlist entry.
2. **Lightweight stemming.** ``_stem_candidates`` emits the original
   token plus a conservative ASCII stem (strip trailing ``s/es/ing/ed/ea``).
   When the original has df=0 but the stem has df>0, the stem replaces
   the original in the output — recovers Spanish imperatives like
   ``fixea -> fix``.
3. **Technical-term allowlist.** A small set of abbreviations
   (``llm``, ``api``, ``mcp``, ``fts5``, ``sqlite``, ``bm25``, ...) is
   protected from the df=0 penalty so low-corpus-frequency technical
   tokens still surface.
4. **Synonym expansion.** ``_SYNONYMS`` bidirectionally maps common
   project terms (``claim<->claims``, ``hook<->hooks``, ...) so that the
   tokenizer picks whichever side has higher df.

Latin-lookaround boundary pattern preserved from the classify-hook work
so accented Spanish words aren't split mid-character.
"""
from __future__ import annotations

import logging
import math
import re
import sqlite3
from collections import Counter
from functools import lru_cache

logger = logging.getLogger(__name__)

_MIN = 3
_WORD = re.compile(r"[A-Za-z\u00c0-\u024f][A-Za-z0-9\u00c0-\u024f_-]{2,}")
_URL = re.compile(r"https?://\S+")
_PATH = re.compile(r"[A-Za-z]:\\\\\S+|/[\w./-]{4,}")
_CODE_FENCE = re.compile(r"```[\s\S]*?```")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
# A word shaped like an identifier (digit, underscore, hyphen, camelCase): one exact match is evidence enough.
_IDENT_SHAPE = re.compile(r"\d|_|-|[a-z][A-Z]")

# Magnitude of the df=0 penalty. With typical IDF in [3, 10], a penalty
# of 8.0 pushes any df=0 token below every df>0 token *unless* it is in
# the technical-term allowlist (which gets a partial offset).
_DF_ZERO_PENALTY = 8.0

# Trailing-suffix stem fallbacks, longest first (order matters — strip
# "ing" before "g"). Purely ASCII; Spanish accents have no trailing-s
# concerns. Conservative: only strip when residual length >= 3.
#
# English verb/plural suffixes first, then a small set of Spanish clitic
# pronouns that commonly attach to imperatives ("explicamelo" -> "explica").
# The clitics are intentionally conservative — they're all ≥2 chars so
# random short words aren't sliced, and ``_best_form`` only picks the
# stem when it has strictly higher df than the original, so we never
# replace a real corpus word with a worse stem.
_STEM_SUFFIXES = (
    "ings", "ing", "ies", "ied", "ed", "es", "ea", "s",
    # Spanish clitic pronoun clusters attached to imperatives/infinitives.
    "melo", "mela", "nosla", "noslo", "selo", "sela",
    "me", "te", "se", "lo", "la", "nos",
)

# Technical terms that should NEVER be penalised for low df — they are
# real signals even if the corpus doesn't mention them often yet.
_TECH_TERMS = frozenset({
    "llm", "llms", "api", "apis", "mcp", "fts5", "sqlite", "sql", "bm25",
    "roc", "ml", "ai", "http", "https", "json", "yaml", "toml", "csv",
    "afip", "ci", "cd", "db", "idf", "lru", "utf", "tpm", "rpm", "rpd",
    "rps", "cors", "csrf", "xss", "jwt", "oauth", "ssl", "tls", "dns",
    "tcp", "udp", "cpu", "ram", "gpu", "ssd", "os", "ui", "ux", "dom",
    "css", "scss", "tsx", "jsx", "vue", "svelte", "npm", "pip", "uv",
    "wal", "orm", "sdk", "cli", "tui", "mcp", "nlp", "rag", "gnn",
    "lstm", "cnn", "bert", "gpt", "vllm", "ollama", "fastapi", "django",
    "flask", "qdrant", "faiss", "hnsw",
})

# Small bidirectional synonym map. Each key maps to its preferred
# alternates. Used only when the original has strictly lower df.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "claim": ("claims",),
    "claims": ("claim",),
    "hook": ("hooks",),
    "hooks": ("hook",),
    "entity": ("entities",),
    "entities": ("entity",),
    "gotcha": ("gotchas",),
    "gotchas": ("gotcha",),
    "scope": ("scopes",),
    "scopes": ("scope",),
    "prompt": ("prompts",),
    "prompts": ("prompt",),
    "tool": ("tools",),
    "tools": ("tool",),
    "agent": ("agents",),
    "agents": ("agent",),
    "pattern": ("patterns",),
    "patterns": ("pattern",),
    "token": ("tokens",),
    "tokens": ("token",),
    "model": ("models",),
    "models": ("model",),
    "test": ("tests",),
    "tests": ("test",),
    "error": ("errors",),
    "errors": ("error",),
    "fix": ("fixes", "fixed", "fixing"),
    "fixes": ("fix",),
    "fixed": ("fix",),
    "fixing": ("fix",),
}

_STOP = frozenset((
    # Spanish function words + chat filler
    "a al algo algun alguna algunas algunos aquel aqui asi aun aunque cada "
    "como con contra cosa cual cuando dale de del desde donde dos el ella "
    "ellos en entre era eran eres es esa ese eso esos esta estan estar este "
    "esto estos fue fuera fueron ha haber habia hace hacer hacia hasta hay "
    "hola hoy las le les lo los mas me mi mis misma mismo mucho muchos muy "
    "nada ni no nos nosotros nuestra nuestro otra otras otro otros para "
    "pero poco por porque puede puedo que se segun ser si sin sobre solo "
    "son soy su sus tal tambien tan tanto te tener tiene tienen toda todas "
    "todo todos tus una unas unos vamos van ver vos ya yo bueno che dale "
    "gracias hagamos hagamoslo opinas fijate mira sabes hacelo correr "
    "tenemos tenes estamos seria sera voy vas decir dice dijo entiendo "
    "ahora antes despues luego nunca siempre podes queres creo "
    # English function words + filler
    "the and or but if then else when while is are was were be been being "
    "have has had do does did can could should would will shall may might "
    "must of in on at to for with from by about as into like through this "
    "that these those you we they he she it my your our their his her its "
    "what which who whom whose why how not yes now just very also even "
    "still only all any some more most less many much ok okay thanks thx "
    "please continue left off where working"
).split())


def _path_identifiers(match: re.Match) -> str:
    """A path keeps its last two segments (directory + file stem): those are the identifiers."""
    parts = [p for p in re.split(r"[/\\]", match.group(0)) if p]
    if parts:
        parts[-1] = re.sub(r"\.\w{1,5}$", "", parts[-1])
    return " " + " ".join(parts[-2:]) + " "


def _strip(text: str) -> str:
    """Drop URLs and fenced code; keep inline-code contents and path identifiers (`fstrim`, a/b/context_hook.py)."""
    text = _CODE_FENCE.sub(" ", _URL.sub(" ", text))
    return _PATH.sub(_path_identifiers, _CODE_SPAN.sub(r" \1 ", text))


def identifier_tokens(raw: str) -> frozenset[str]:
    """Lower-cased tokens of ``raw`` that are exact identifiers: inline code, path segments, identifier-shaped words."""
    base = _CODE_FENCE.sub(" ", _URL.sub(" ", raw))
    quoted = " ".join(_CODE_SPAN.findall(base)) + " " + " ".join(m.group(0) for m in _PATH.finditer(base))
    found = {m.group(0).lower() for m in _WORD.finditer(_strip(quoted))}
    found.update(m.group(0).lower() for m in _WORD.finditer(_strip(base)) if _IDENT_SHAPE.search(m.group(0)))
    return frozenset(t for t in found if len(t) >= _MIN and t not in _STOP and not t.isdigit())


def _candidate_tokens(raw: str) -> list[str]:
    """Tokenize -> lowercase -> drop stopwords + too-short + pure-digits."""
    out: list[str] = []
    for m in _WORD.finditer(_strip(raw)):
        tok = m.group(0).lower()
        if len(tok) < _MIN or tok in _STOP or tok.isdigit():
            continue
        out.append(tok)
    return out


def _stem(tok: str) -> str | None:
    """Return an ASCII stem for ``tok`` or None if none applies.

    Strips the first matching suffix in ``_STEM_SUFFIXES`` provided the
    residual is at least 3 chars long and differs from the input.
    """
    lower = tok.lower()
    for suffix in _STEM_SUFFIXES:
        if lower.endswith(suffix) and len(lower) - len(suffix) >= 3:
            stem = lower[: -len(suffix)]
            if stem != lower:
                return stem
    return None


# Claims in these statuses can never match a live recall query (they are
# retired or replaced), so they must not contribute to document frequencies
# or inflate ``total_docs``. Restricting the scan keeps IDF aligned with what
# recall can actually return and shrinks the per-prompt corpus re-tokenize.
_NON_CANONICAL_STATUSES = ("archived", "superseded")


def read_text_generation(db_path: str) -> int:
    """Cache key for the corpus statistics: changes only when they can (migration 28).

    ``text_generation`` advances on claim inserts, deletes, text or status changes and
    alias changes. A database without it falls back to ``corpus_generation``, which
    is coarser (it also moves on confidence and validation writes) but never stale.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0
    try:
        row = conn.execute("SELECT value FROM cache_meta WHERE key = 'text_generation'").fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    if row is not None:
        return int(row[0])
    from memorymaster.recall.query_cache import read_generation

    return read_generation(db_path)


@lru_cache(maxsize=16)
def _corpus_stats(
    db_path: str, generation: int | None = None
) -> tuple[int, dict[str, int]]:
    """Return (total_docs, {token: doc_frequency}) for IDF. Read-only.

    Only canonical claims (everything except ``archived``/``superseded``)
    are counted: those statuses are never returned by recall, so including
    them would skew IDF toward dead tokens and waste tokenization on rows
    that can never match.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.debug("recall_tokenizer: cannot open %s ro: %s", db_path, exc)
        return (0, {})
    placeholders = ",".join("?" * len(_NON_CANONICAL_STATUSES))
    try:
        total = int(
            conn.execute(
                f"SELECT COUNT(*) FROM claims WHERE status NOT IN ({placeholders})",
                _NON_CANONICAL_STATUSES,
            ).fetchone()[0]
        )
        rows = conn.execute(
            f"SELECT text FROM claims "
            f"WHERE text IS NOT NULL AND status NOT IN ({placeholders})",
            _NON_CANONICAL_STATUSES,
        )
        # Same document frequencies as counting _candidate_tokens per claim, with
        # less work per claim: _strip can only change text holding a URL, a path or
        # code, and each claim counts a token once, so dedupe first and let
        # Counter do the counting in C. Cold recall scanned ~46k live claims.
        # _WORD matches start with a letter and are at least _MIN long, and lower()
        # neither shortens text nor turns a letter into a digit, so the length and
        # digit filters of _candidate_tokens cannot drop anything here.
        counts: Counter[str] = Counter()
        findall, stop, lower = _WORD.findall, _STOP, str.lower
        for (text,) in rows:
            if not text:
                continue
            if "http" in text or "/" in text or "`" in text or "\\" in text:
                text = _strip(text)
            tokens = set(map(lower, findall(text)))
            tokens.difference_update(stop)
            counts.update(tokens)
        return (total, dict(counts))
    except sqlite3.Error as exc:
        logger.debug("recall_tokenizer: corpus scan failed: %s", exc)
        return (0, {})
    finally:
        conn.close()


@lru_cache(maxsize=16)
def _alias_set(db_path: str, generation: int | None = None) -> frozenset[str]:
    """Return lowercased entity aliases. Empty if table missing."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return frozenset()
    try:
        rows = conn.execute("SELECT alias FROM entity_aliases").fetchall()
        return frozenset(str(r[0]).lower() for r in rows if r and r[0])
    except sqlite3.Error:
        return frozenset()
    finally:
        conn.close()


def _best_form(tok: str, df: dict[str, int]) -> str:
    """Pick the highest-df surface form among ``tok``, its stem, and synonyms.

    Does not invent tokens — only returns a form known to appear in the
    corpus if at least one such form exists. Falls back to the original
    ``tok`` otherwise so behaviour is unchanged when df is unavailable.
    """
    if not df:
        return tok
    candidates: list[str] = [tok]
    stem = _stem(tok)
    if stem:
        candidates.append(stem)
    for syn in _SYNONYMS.get(tok, ()):
        candidates.append(syn)
    # De-dup while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    # Pick the form with the highest df; ties go to the original.
    best = tok
    best_df = df.get(tok, 0)
    for c in ordered[1:]:
        d = df.get(c, 0)
        if d > best_df:
            best_df = d
            best = c
    return best


def _resolve_tokens(raw_prompt: str, db_path: str):
    """Candidate tokens resolved to their best corpus form, with the corpus statistics used to score them.

    Returns ``(resolved, total_docs, df, aliases)`` where ``resolved`` maps best form -> (source token,
    first index) in first-seen order; empty when the prompt has no candidate token.
    """
    tokens = _candidate_tokens(raw_prompt)
    if not tokens:
        return {}, 0, {}, frozenset()

    generation = read_text_generation(db_path)
    total_docs, df = _corpus_stats(db_path, generation)
    aliases = _alias_set(db_path, generation)

    # Resolve each unique token to the best-matching corpus form (stem
    # or synonym fallback). Preserve first-seen order.
    order: dict[str, int] = {}
    for i, tok in enumerate(tokens):
        order.setdefault(tok, i)

    resolved: dict[str, tuple[str, int]] = {}  # best_form -> (source_tok, first_idx)
    for tok, first_idx in order.items():
        best = _best_form(tok, df)
        # Keep the earliest index if the same best-form appears twice.
        if best not in resolved or first_idx < resolved[best][1]:
            resolved[best] = (tok, first_idx)
    return resolved, total_docs, df, aliases


def _score_tokens(resolved, total_docs, df, aliases, idents: frozenset[str]) -> list[tuple[str, float]]:
    """Score by smoothed IDF, plus boosts for alias / technical term / identifier / length, penalty for
    df==0 unless whitelisted, stable tiebreak on first-seen index. Best first."""
    scored: list[tuple[str, float]] = []
    for best_form, (source, first_idx) in resolved.items():
        freq = df.get(best_form, 0) if total_docs > 0 else 1
        if total_docs > 0:
            idf = math.log((total_docs + 1) / (freq + 1)) + 1
        else:
            idf = 1.0
        score = idf
        if best_form in aliases:
            score += 2.0
        if best_form in idents or source in idents:
            score += 1.5  # an identifier the author quoted or shaped (`fstrim`, a_b, v2): never crowded out
        if best_form in _TECH_TERMS:
            # Technical abbreviations always escape the df=0 penalty
            # and get a small boost on top of IDF so short but real
            # terms like "llm" or "mcp" surface.
            score += 1.5
        elif total_docs > 0 and freq == 0:
            # df=0 tokens cannot match any document: demote hard.
            score -= _DF_ZERO_PENALTY
        if len(best_form) >= 6:
            score += 0.1
        score -= 0.001 * first_idx
        scored.append((best_form, score))
    scored.sort(key=lambda p: p[1], reverse=True)
    return scored


def extract_query_tokens(raw_prompt: str, db_path: str, max_tokens: int = 6) -> str:
    """Extract up to ``max_tokens`` salient tokens, space-joined."""
    if not raw_prompt or not raw_prompt.strip() or max_tokens <= 0:
        return ""
    resolved, total_docs, df, aliases = _resolve_tokens(raw_prompt, db_path)
    if not resolved:
        return ""

    # Short prompt: keep all unique resolved tokens in first-seen order.
    if len(resolved) <= max_tokens:
        by_idx = sorted(resolved.items(), key=lambda kv: kv[1][1])
        return " ".join(k for k, _ in by_idx[:max_tokens])

    scored = _score_tokens(resolved, total_docs, df, aliases, identifier_tokens(raw_prompt))
    return " ".join(tok for tok, _ in scored[:max_tokens])


def salient_terms(raw_prompt: str, db_path: str, max_terms: int = 12) -> list[str]:
    """Up to ``max_terms`` salient terms of the WHOLE prompt, most salient first (identifiers included)."""
    if not raw_prompt or not raw_prompt.strip() or max_terms <= 0:
        return []
    resolved, total_docs, df, aliases = _resolve_tokens(raw_prompt, db_path)
    if not resolved:
        return []
    scored = _score_tokens(resolved, total_docs, df, aliases, identifier_tokens(raw_prompt))
    return [tok for tok, _ in scored[:max_terms]]


def centered_excerpt(text: str, query: str, width: int = 400) -> str:
    """The ``width``-character window of ``text`` that holds the most distinct query terms.

    Matching is by a stem-tolerant prefix (first 5 letters of a long term), so a porter-stemmed index hit
    ("replicated" for "replication") still centres the window. Cut points move to word boundaries and a cut
    side gets an ellipsis. No query term found -> the start of ``text``.
    """
    text = text.strip()
    if len(text) <= width:
        return text
    needles = {t[:5] if len(t) >= 6 else t for t in _candidate_tokens(query)}
    low = text.lower()
    hits: list[tuple[int, str]] = []
    for needle in needles:
        pos = low.find(needle)
        for _ in range(40):  # bounded: a ubiquitous term must not make this quadratic
            if pos < 0:
                break
            hits.append((pos, needle))
            pos = low.find(needle, pos + 1)
    if not hits:
        return text[:width].rstrip() + "…"
    best_start, best_count = 0, -1
    for pos, _needle in sorted(hits):
        start = max(0, pos - width // 6)
        count = len({n for p, n in hits if start <= p < start + width})
        if count > best_count:
            best_start, best_count = start, count
    start, end = best_start, min(len(text), best_start + width)
    if start > 0 and (space := text.find(" ", start, start + 20)) != -1:
        start = space + 1
    if end < len(text) and (space := text.rfind(" ", end - 20, end)) != -1:
        end = space
    return ("…" if start > 0 else "") + text[start:end].strip() + ("…" if end < len(text) else "")
