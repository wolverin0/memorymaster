"""Machine traffic must not trigger prompt recall (T-2273).

Measured 2026-11-09 (``scripts/delivery_metrics.py``): 51.6% of recall returns answered machine traffic,
not human demand: transport notices (34.1%) and our own LLM pipelines (17.5%). Two cheap gates, shared by
every recall adapter (the Claude template, which the Codex adapter also runs; OpenCode; agy):

* **internal run**: a process our own pipelines spawn (dreaming extractor, steward, judges, precompact
  residual) inherits ``MEMORYMASTER_INTERNAL_RUN=1``; the Codex flavour ``MEMORYMASTER_INTERNAL_CODEX=1`` is
  honoured too. Such a session never recalls.
* **transport notice**: the WHOLE prompt has the exact shape of a harness/orchestrator notice (see
  :data:`TRANSPORT_NOTICES`). Not a substring, not a prefix: a notice followed (or preceded) by anything the
  agent could act on is a human request and still recalls. Coordinator task briefs are legitimate demand and
  are not in the list.

Rollback per adapter: ``MEMORYMASTER_RECALL_MACHINE_FILTER_<ADAPTER>=0`` (``CLAUDE``, ``CODEX``, ``OPENCODE``,
``AGY``) turns both gates off for that adapter only, including the Codex adapter's pre-stdin internal check.

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


class Notice(NamedTuple):
    start: str  # prefix regex: how the metrics script classifies a stored query head
    whole: str  # full-shape regex: the ENTIRE (stripped) prompt must match it for the hooks to skip


# THE one list of transport notices (the harness/orchestrator pushing a notice into a session).
TRANSPORT_NOTICES = (
    # Orca: "You have N orchestration message(s). Run `orca orchestration check --run run_<id>`."
    Notice(r"You have \d+ orchestration message",
           r"You have \d+ orchestration messages?(?:\(s\))?\. Run `[^`\n]+`\.?"),
    # A bare ficha ping tag; a ping with a body sentence is addressed to the agent and recalls.
    Notice(r"\[ficha-ping", r"\[ficha-ping[^\]\n]*\]"),
    Notice(r"<task-notification", r"<task-notification\b[^>]*>.*</task-notification>"),
    # A bare opening tag, or an element closed at the very end.
    Notice(r"<subagent", r"<subagent\b[^>]*>(?:.*</subagent>)?"),
    # Harness envelope: header line, then a body that ends with the task-notification element.
    Notice(r"\[SYSTEM NOTIFICATION", r"\[SYSTEM NOTIFICATION[^\]\n]*\].*</task-notification>"),
)
TRANSPORT_NOTICE_PATTERNS = tuple(re.compile(n.start) for n in TRANSPORT_NOTICES)
_WHOLE = tuple(re.compile(n.whole, re.DOTALL) for n in TRANSPORT_NOTICES)


def internal_run(environ: Mapping[str, str] = os.environ) -> bool:
    """Whether this process was spawned by one of our own pipelines."""
    return any(environ.get(name, "").strip().lower() in _TRUE for name in INTERNAL_ENVS)


def filter_enabled(adapter: str, environ: Mapping[str, str] = os.environ) -> bool:
    return environ.get(FILTER_ENV_PREFIX + adapter.upper(), "").strip().lower() not in _FALSE


def is_transport_notice(prompt: object) -> bool:
    """Whether the whole prompt is a transport notice (full-shape match, never a substring or prefix)."""
    if not isinstance(prompt, str):
        return False
    text = prompt.strip()
    return any(whole.fullmatch(text) for whole in _WHOLE)


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
