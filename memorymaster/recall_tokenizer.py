"""Recall tokenizer — extract salient FTS5 tokens from a raw prompt.

Real prompts are 50–500 chars of mixed Spanish/English. FTS5 AND-joins
every token, so full-prompt queries almost never hit. Fix: extract up to
``max_tokens`` salient tokens (stopword filter + IDF + entity-alias boost).
Stdlib only, read-only DB, per-process LRU cache.

See ``artifacts/retrieval-eval-2026-04-22.md`` for the motivating audit.
"""
from __future__ import annotations

import logging
import math
import re
import sqlite3
from functools import lru_cache

logger = logging.getLogger(__name__)

_MIN = 3
_WORD = re.compile(r"[A-Za-z\u00c0-\u024f][A-Za-z0-9\u00c0-\u024f_-]{2,}")
_URL = re.compile(r"https?://\S+")
_PATH = re.compile(r"[A-Za-z]:\\\\\S+|/[\w./-]{4,}")
_CODE = re.compile(r"`[^`]+`|```[\s\S]*?```")

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


def _strip(text: str) -> str:
    return _PATH.sub(" ", _CODE.sub(" ", _URL.sub(" ", text)))


def _candidate_tokens(raw: str) -> list[str]:
    """Tokenize → lowercase → drop stopwords + too-short + pure-digits."""
    out: list[str] = []
    for m in _WORD.finditer(_strip(raw)):
        tok = m.group(0).lower()
        if len(tok) < _MIN or tok in _STOP or tok.isdigit():
            continue
        out.append(tok)
    return out


@lru_cache(maxsize=8)
def _corpus_stats(db_path: str) -> tuple[int, dict[str, int]]:
    """Return (total_docs, {token: doc_frequency}) for IDF. Read-only."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.debug("recall_tokenizer: cannot open %s ro: %s", db_path, exc)
        return (0, {})
    try:
        total = int(conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0])
        df: dict[str, int] = {}
        for (text,) in conn.execute("SELECT text FROM claims WHERE text IS NOT NULL"):
            seen: set[str] = set()
            for tok in _candidate_tokens(text or ""):
                if tok in seen:
                    continue
                seen.add(tok)
                df[tok] = df.get(tok, 0) + 1
        return (total, df)
    except sqlite3.Error as exc:
        logger.debug("recall_tokenizer: corpus scan failed: %s", exc)
        return (0, {})
    finally:
        conn.close()


@lru_cache(maxsize=8)
def _alias_set(db_path: str) -> frozenset[str]:
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


def extract_query_tokens(raw_prompt: str, db_path: str, max_tokens: int = 6) -> str:
    """Extract up to ``max_tokens`` salient tokens, space-joined."""
    if not raw_prompt or not raw_prompt.strip() or max_tokens <= 0:
        return ""
    tokens = _candidate_tokens(raw_prompt)
    if not tokens:
        return ""
    # Short prompt: keep all unique tokens in first-seen order.
    unique = {t for t in tokens}
    if len(unique) <= max_tokens:
        seen: set[str] = set()
        ordered: list[str] = []
        for tok in tokens:
            if tok not in seen:
                seen.add(tok)
                ordered.append(tok)
        return " ".join(ordered[:max_tokens])
    # Score by smoothed IDF + alias boost + length + stable tiebreak.
    total_docs, df = _corpus_stats(db_path)
    aliases = _alias_set(db_path)
    order: dict[str, int] = {}
    for i, tok in enumerate(tokens):
        order.setdefault(tok, i)
    scored: list[tuple[str, float]] = []
    for tok, first_idx in order.items():
        freq = df.get(tok, 1) if total_docs > 0 else 1
        idf = math.log((total_docs + 1) / (freq + 1)) + 1 if total_docs > 0 else 1.0
        score = idf + (2.0 if tok in aliases else 0.0) + (0.1 if len(tok) >= 6 else 0.0) - 0.001 * first_idx
        scored.append((tok, score))
    scored.sort(key=lambda p: p[1], reverse=True)
    return " ".join(tok for tok, _ in scored[:max_tokens])
