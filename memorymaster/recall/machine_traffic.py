"""Machine traffic must not trigger prompt recall (T-2273).

Measured 2026-11-09 (``scripts/delivery_metrics.py``): 51.6% of recall returns answered machine traffic,
not human demand: transport notices (34.1%) and our own LLM pipelines (17.5%). Two cheap gates, shared by
every recall adapter (the Claude template, which the Codex adapter also runs; OpenCode; agy):

* **internal run**: a process our own pipelines spawn (dreaming extractor, steward, judges, precompact
  residual) inherits ``MEMORYMASTER_INTERNAL_RUN=1``; the Codex flavour ``MEMORYMASTER_INTERNAL_CODEX=1`` is
  honoured too. Such a session never recalls.
* **transport notice**: the WHOLE prompt is a harness/orchestrator notice (see :data:`TRANSPORT_NOTICES`).
  Whole-prompt, not substring or even prefix: a human prompt that merely quotes a notice, or one that
  continues after it, still gets recall. Coordinator task briefs are legitimate demand and are not here.

Rollback per adapter: ``MEMORYMASTER_RECALL_MACHINE_FILTER_<ADAPTER>=0`` (``CLAUDE``, ``CODEX``, ``OPENCODE``,
``AGY``) turns both gates off for that adapter only.

Standard library only (the hook process imports it on every prompt); never raises; logs the category of each
skip, never the prompt text.
"""

from __future__ import annotations

import os
import re
from typing import Mapping, NamedTuple

INTERNAL_ENVS = ("MEMORYMASTER_INTERNAL_RUN", "MEMORYMASTER_INTERNAL_CODEX")
FILTER_ENV_PREFIX = "MEMORYMASTER_RECALL_MACHINE_FILTER_"
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
PLAIN_NOTICE_MAX_CHARS = 600  # a bare notice is short; a longer one is a human writing about it


class Notice(NamedTuple):
    start: str        # regex matched at the (left-stripped) start of the prompt
    end: str | None   # regex the prompt must END with (closing tag/bracket); None = bounded plain notice


# THE one list of transport notices (the harness/orchestrator pushing a notice into a session). The metric
# script classifies stored query heads with the ``start`` patterns; the hooks skip only whole-prompt notices.
TRANSPORT_NOTICES = (
    Notice(r"You have \d+ orchestration message", None),
    Notice(r"\[ficha-ping", r"\]"),
    Notice(r"<task-notification", r"</task-notification>"),
    Notice(r"<subagent", r">"),
    Notice(r"\[SYSTEM NOTIFICATION", None),
)
TRANSPORT_NOTICE_PATTERNS = tuple(re.compile(n.start) for n in TRANSPORT_NOTICES)
_ENDS = tuple(re.compile(n.end + r"\s*\Z") if n.end else None for n in TRANSPORT_NOTICES)


def internal_run(environ: Mapping[str, str] = os.environ) -> bool:
    """Whether this process was spawned by one of our own pipelines."""
    return any(environ.get(name, "").strip().lower() in _TRUE for name in INTERNAL_ENVS)


def filter_enabled(adapter: str, environ: Mapping[str, str] = os.environ) -> bool:
    return environ.get(FILTER_ENV_PREFIX + adapter.upper(), "").strip().lower() not in _FALSE


def is_transport_notice(prompt: object) -> bool:
    """Whether the whole prompt is a transport notice (anchored; never a substring)."""
    if not isinstance(prompt, str):
        return False
    text = prompt.strip()
    for start, end in zip(TRANSPORT_NOTICE_PATTERNS, _ENDS):
        if start.match(text):
            if end is not None:
                return bool(end.search(text))
            return len(text) <= PLAIN_NOTICE_MAX_CHARS and "?" not in text  # a notice asks nothing
    return False


def machine_skip(
    prompt: object, adapter: str, *, session: str = "", environ: Mapping[str, str] = os.environ,
) -> str | None:
    """Category (``internal_run`` / ``transport_notice``) when this prompt must not reach the ranker, else None.

    Logs ``hook=recall event=skip reason=machine-traffic category=... adapter=...`` (no prompt text).
    """
    try:
        if not filter_enabled(adapter, environ):
            return None
        category = "internal_run" if internal_run(environ) else "transport_notice" if is_transport_notice(prompt) else None
        if category:
            from memorymaster.core.hook_log import log_hook

            log_hook("recall", "skip", session=session, reason="machine-traffic", category=category, adapter=adapter)
        return category
    except Exception:
        return None
