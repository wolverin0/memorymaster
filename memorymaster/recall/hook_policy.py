"""How much the prompt-recall hook injects, and where it stays silent (T-1351).

Measured 2026-10-06..13: in 7 days the hook injected 3297 claims into 698
prompts (~5 claims, ~5300 characters per prompt) and the model cited 14 of
them (0.42%); the same never-cited claims came back up to ~50 times a week.
Operator decision 2026-10-13, implemented here:

* at most :func:`max_claims` (default 2) claims per prompt, dense and lexical;
* a claim already injected in a session is not injected again in that session;
* the hook can be turned off per lane/cwd from a config file, to run an A/B;
* a trivial prompt ("ok", "dale", "gracias", a lone emoji) gets no injection
  (T-1433): see :func:`is_trivial_prompt`.

Standard library only: the hook process imports this on every prompt. Nothing
here stores prompt text or claim text, and every failure degrades to "no
effect" (hook enabled, nothing remembered), never to an exception.

Turn the hook off for some projects (default is on everywhere) by writing
``~/.memorymaster/recall-hook.json``::

    {"disabled_cwds": ["whatsappbot-final", "whatsappbot", "bot-rf-optimizer"]}

An entry without a path separator matches any folder of the session's cwd
(so worktrees under that repo are off too); an entry with separators matches
that path and everything under it. ``{"enabled": false}`` turns it off
everywhere. The file is read on every prompt: no restart is needed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from pathlib import Path

from memorymaster.core.text_safety import sanitize_text

ENABLED_ENV = "MEMORYMASTER_RECALL_HOOK"
CONFIG_ENV = "MEMORYMASTER_RECALL_HOOK_CONFIG"
MAX_CLAIMS_ENV = "MEMORYMASTER_RECALL_HOOK_MAX_CLAIMS"
LEXICAL_MIN_ENV = "MEMORYMASTER_RECALL_LEXICAL_MIN_RELEVANCE"
SESSION_DEDUP_ENV = "MEMORYMASTER_RECALL_SESSION_DEDUP"
SEEN_DIR_ENV = "MEMORYMASTER_RECALL_SEEN_DIR"

DEFAULT_MAX_CLAIMS = 2
DEFAULT_LEXICAL_MIN_RELEVANCE = 0.5  # measured: 0.5 costs ~3% nDCG; 1 costs 18%, 2 costs 53% (no precision gain)
SEEN_TTL_SECONDS = 12 * 3600
SEEN_MAX_IDS = 200  # per session; the oldest are forgotten first
SEEN_MAX_FILES = 400  # sessions kept on disk
_FALSE = {"0", "false", "no", "off"}


def _config_path() -> Path:
    raw = os.environ.get(CONFIG_ENV)
    return Path(raw) if raw else Path.home() / ".memorymaster" / "recall-hook.json"


def _config() -> dict:
    try:
        data = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _parts(path: str) -> list[str]:
    return [p for p in path.replace("\\", "/").lower().split("/") if p]


def _matches(entry: str, cwd: str) -> bool:
    wanted, have = _parts(entry), _parts(cwd)
    if not wanted:
        return False
    if len(wanted) == 1 and ":" not in wanted[0]:
        return wanted[0] in have  # a bare folder name matches anywhere in the cwd
    return have[: len(wanted)] == wanted


def disabled_for(cwd: object) -> bool:
    """Whether the prompt hook must stay silent for a session running in ``cwd``."""
    if os.environ.get(ENABLED_ENV, "").strip().lower() in _FALSE:
        return True
    config = _config()
    if config.get("enabled") is False:
        return True
    entries = config.get("disabled_cwds")
    if not isinstance(cwd, str) or not cwd or not isinstance(entries, list):
        return False
    return any(isinstance(entry, str) and _matches(entry, cwd) for entry in entries)


def max_claims() -> int:
    try:
        return max(1, int(os.environ.get(MAX_CLAIMS_ENV, DEFAULT_MAX_CLAIMS)))
    except ValueError:
        return DEFAULT_MAX_CLAIMS


def lexical_min_relevance() -> float:
    """Lowest lexical relevance a prompt-hook row may have (unnormalized BM25-based score); 0 keeps every row."""
    try:
        return float(os.environ.get(LEXICAL_MIN_ENV, DEFAULT_LEXICAL_MIN_RELEVANCE))
    except ValueError:
        return DEFAULT_LEXICAL_MIN_RELEVANCE


# --- trivial prompts (T-1433) -------------------------------------------------------------

# Spanish + English acknowledgements, assents and "keep going" fillers, accent-folded and
# lower-cased. A prompt is trivial only when EVERY word is one of these: a single other word
# ("ok fix the importer", "seguí con T-1433") keeps recall.
_ACK_WORDS = frozenset("""
ok okay okey oka k kk vale dale listo ya bueno buenisimo genial perfecto perfect excelente
barbaro joya claro exacto correcto cierto entendido entiendo hecho
si sip sep seh no nop nope yes yep yeah yup ye sure
gracias gracia grax muchas mil thanks thank thx ty tks cheers you
good great cool nice fine done got it sounds right
bien muy todo eso esa ese lo por favor please pls
continua continue continuar continuemos segui seguir segi sigue seguimos sigamos
proceed go ahead next dale adelante avanza avanzar
ahora ahi aca aqui entonces tambien
""".split())
_WORD = re.compile(r"[^\W_]+")
_IDENTIFIER = re.compile(r"\d|[^\W]_|_[^\W]|\w[./\\-]\w")
_TRIVIAL_MAX_WORDS = 6


def _fold(word: str) -> str:
    decomposed = unicodedata.normalize("NFKD", word.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _is_ack(word: str) -> bool:
    folded = _fold(word)
    # "okkk", "daleee", "goood": a run of 3+ letters may be one or two in the real word.
    return folded in _ACK_WORDS or any(
        re.sub(r"(.)\1{2,}", lambda m: m.group(1) * keep, folded) in _ACK_WORDS for keep in (1, 2)
    )


def is_trivial_prompt(prompt: object) -> bool:
    """Whether a prompt carries nothing to recall for: empty, symbols/emoji only, or only acknowledgements.

    Conservative: any digit, path/identifier shape or word outside the Spanish/English acknowledgement
    list makes the prompt non-trivial, as does a prompt longer than a few words, so "why 500?" or
    "T-1433 status" still get recall. Never raises.
    """
    if not isinstance(prompt, str):
        return False
    text = prompt.strip()
    if _IDENTIFIER.search(text):
        return False
    words = _WORD.findall(text)
    if not words:
        return True  # nothing but punctuation, symbols or emoji
    return len(words) <= _TRIVIAL_MAX_WORDS and all(_is_ack(w) for w in words)


# --- claims already injected in a session -------------------------------------------------


def session_id_of(hook_data: object) -> str | None:
    """The session id of a hook payload, or None when this call must not deduplicate."""
    if os.environ.get(SESSION_DEDUP_ENV, "").strip().lower() in _FALSE:
        return None
    session = hook_data.get("session_id") if isinstance(hook_data, dict) else None
    return session if isinstance(session, str) and session else None


def _seen_dir() -> Path:
    raw = os.environ.get(SEEN_DIR_ENV)
    return Path(raw) if raw else Path.home() / ".memorymaster" / "hook_state" / "recall_seen"


def _seen_path(session_id: str) -> Path:
    return _seen_dir() / (hashlib.sha256(sanitize_text(session_id).encode("utf-8")).hexdigest() + ".json")


def _load(session_id: str, now: float) -> list[int]:
    try:
        state = json.loads(_seen_path(session_id).read_text(encoding="utf-8"))
        if not 0 <= now - float(state["ts"]) < SEEN_TTL_SECONDS:
            return []
        return [int(i) for i in state["ids"]]
    except (OSError, ValueError, TypeError, KeyError):
        return []


def seen_ids(session_id: str, *, now: float | None = None) -> frozenset[int]:
    """Claim ids already injected in this session, unexpired; empty on any problem."""
    return frozenset(_load(session_id, time.time() if now is None else now))


def remember(session_id: str, ids: list[int], *, now: float | None = None) -> None:
    """Add ``ids`` to the session's injected set (bounded, expiring); never raises."""
    if not ids:
        return
    now = time.time() if now is None else now
    path = _seen_path(session_id)
    temporary = None
    try:
        merged = [*(i for i in _load(session_id, now) if i not in ids), *ids]  # newest last
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False,
                                         suffix=".tmp") as handle:
            temporary = Path(handle.name)
            json.dump({"ts": now, "ids": merged[-SEEN_MAX_IDS:]}, handle)
        os.replace(temporary, path)
        temporary = None
        _prune(path.parent, now)
    except OSError:
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _prune(directory: Path, now: float) -> None:
    """Drop expired sessions, then the oldest beyond the cap. Cheap while the dir is small."""
    try:
        entries = [entry for entry in os.scandir(directory) if entry.name.endswith((".json", ".tmp"))]
        if len(entries) <= SEEN_MAX_FILES // 2:
            return
        aged = sorted(((entry.stat().st_mtime, entry.path) for entry in entries))
        keep = SEEN_MAX_FILES // 2
        for index, (mtime, file) in enumerate(aged):
            if now - mtime > SEEN_TTL_SECONDS or index < len(aged) - keep:
                os.unlink(file)
    except OSError:
        pass


def forget(session_id: str) -> None:
    """A new/compacted session lost the injected context: its claims may be injected again."""
    try:
        _seen_path(session_id).unlink(missing_ok=True)
    except OSError:
        pass
