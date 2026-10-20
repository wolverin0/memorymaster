"""Injection format of the prompt-recall hook (T-1632, AO-0): ``current`` | ``header`` | ``gated``.

Measured 2026-10-12..19: the hook injects claims into a prompt and the model cites them with a strict
``[mm-id]`` in under 1% of them. This module is the A/B switch for *how* the block is presented, chosen by
``MEMORYMASTER_RECALL_FORMAT``:

* ``current`` (default, also for any unknown value): the block exactly as recall rendered it.
* ``header``: a three-line "Decisions you made earlier" header, at most :data:`HEADER_MAX_CLAIMS` claims,
  ranked by lexical overlap with the active task first and the prompt second.
* ``gated``: same ranking, current wrapper, and nothing at all unless some claim shares at least
  :func:`gate_min_shared` meaningful words with the prompt and active task.

The hook receives rendered text only (no scores, no timestamps), so relevance here is lexical overlap and
freshness is not judged. Pure standard library: the hook imports this on every prompt. Nothing here stores
prompt or claim text, and every failure degrades to the unchanged block.
"""

from __future__ import annotations

import os
import re
import unicodedata

FORMAT_ENV = "MEMORYMASTER_RECALL_FORMAT"
GATE_MIN_SHARED_ENV = "MEMORYMASTER_RECALL_GATE_MIN_SHARED"
MODES = ("current", "header", "gated")
HEADER_MAX_CLAIMS = 5
DEFAULT_GATE_MIN_SHARED = 2
HEADER_LINES = (
    "# Decisions you made earlier (cite [mm-id] when you rely on one)",
    "Ranked by relevance to your active task, then to this prompt.",
    "",
)

_BULLET = re.compile(r"^- \[(mm-[0-9a-zA-Z]+(?:~\d+)?)\]")
_WORD = re.compile(r"[a-z0-9]{4,}")
_STOP = frozenset("""
that this with from have what when which there their about should would could into than then them they were
been will your only also some just like more very much here does done make made need want para como pero esto
esta este esos esas cuando donde sobre desde hasta entre porque tambien tiene tienen hacer quiero necesito
quiere estos estas puede pueden todo toda todos todas
""".split())


def format_mode(environ=os.environ) -> str:
    mode = (environ.get(FORMAT_ENV) or "").strip().lower()
    return mode if mode in MODES else "current"


def gate_min_shared(environ=os.environ) -> int:
    try:
        return max(1, int(environ.get(GATE_MIN_SHARED_ENV, DEFAULT_GATE_MIN_SHARED)))
    except ValueError:
        return DEFAULT_GATE_MIN_SHARED


def tokens(text: object) -> frozenset[str]:
    """Meaningful lower-cased, accent-folded words (4+ letters/digits, no stop words)."""
    if not isinstance(text, str):
        return frozenset()
    folded = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    return frozenset(_WORD.findall(folded)) - _STOP


def parse_context(ctx: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Split a rendered recall context into its header lines and ``(claim id, bullet text)`` pairs."""
    header: list[str] = []
    bullets: list[list[str]] = []
    for line in ctx.split("\n"):
        found = _BULLET.match(line)
        if found:
            bullets.append([found.group(1), line])
        elif bullets:
            bullets[-1][1] += "\n" + line
        else:
            header.append(line)
    return header, [(claim_id, text) for claim_id, text in bullets]


def ranked(bullets, prompt: str, task: str):
    """Stable sort: most overlap with the active task first, then with the prompt.

    Rows are ``(task_overlap, prompt_overlap, original_index, claim_id, text)``.
    """
    task_words, prompt_words = tokens(task), tokens(prompt)
    rows = [(len(task_words & tokens(text)), len(prompt_words & tokens(text)), index, claim_id, text)
            for index, (claim_id, text) in enumerate(bullets)]
    rows.sort(key=lambda row: (-row[0], -row[1], row[2]))
    return rows


def gate_passes(top_text: str, prompt: str, task: str) -> bool:
    """Whether the top-ranked claim shares enough meaningful words with the prompt and active task."""
    query = tokens(prompt) | tokens(task)
    return bool(query) and len(query & tokens(top_text)) >= min(gate_min_shared(), len(query))


def apply_format(ctx: str, prompt: str = "", task: str = "", mode: str | None = None) -> str:
    """Return the context to deliver for ``mode`` (env default); ``""`` means inject nothing."""
    try:
        mode = mode or format_mode()
        if mode not in ("header", "gated") or not isinstance(ctx, str):
            return ctx
        header, bullets = parse_context(ctx)
        if not bullets:
            return ctx
        rows = ranked(bullets, prompt, task)
        if mode == "gated":
            if not any(gate_passes(row[4], prompt, task) for row in rows):  # the best match, not the task-ranked first
                return ""
            return "\n".join([*header, *(row[4] for row in rows)])
        return "\n".join([*HEADER_LINES, *(row[4] for row in rows[:HEADER_MAX_CLAIMS])])
    except Exception:  # a format bug must never cost the operator their recall
        return ctx


def active_task_text(cwd: str, max_levels: int = 6) -> str:
    """Title and start of the fresh in-progress task for ``cwd`` (T-1433 rules), else ``""``."""
    if not isinstance(cwd, str) or not cwd.strip():  # abspath('') would be the process cwd, not the session's
        return ""
    try:
        from memorymaster.recall.active_tasks import fresh_active_task

        current = os.path.abspath(cwd)
        for _ in range(max_levels):
            for relative in ("active_tasks.md", os.path.join("vault", "active_tasks.md")):
                candidate = os.path.join(current, relative)
                if os.path.isfile(candidate):
                    title, narrative = fresh_active_task(candidate, cwd=cwd)
                    return f"{title}. {(narrative or '')[:400]}" if title else ""
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    except Exception:
        pass
    return ""
