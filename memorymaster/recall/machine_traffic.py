"""Machine traffic must not trigger prompt recall (T-2273).

Measured 2026-11-09 (``scripts/delivery_metrics.py``): 51.6% of recall returns answered machine traffic,
not human demand: transport notices (34.1%) and our own LLM pipelines (17.5%). Two cheap gates, shared by
every recall adapter (the Claude template, which the Codex adapter also runs; OpenCode; agy):

* **internal run**: a process our own pipelines spawn (dreaming extractor, steward, judges, precompact
  residual) inherits ``MEMORYMASTER_INTERNAL_RUN=1``; the Codex flavour ``MEMORYMASTER_INTERNAL_CODEX=1`` is
  honoured too. Such a session never recalls.
* **transport notice** (the mixed-prompt rule): every notice shape of :data:`TRANSPORT_NOTICES` (Orca mailbox
  notices, ficha pings, task notifications, agent-to-agent messages, the terminal ``From:`` header) is cut out
  of the prompt wherever it sits, so notices glued to other text or stacked still count. The prompt is machine
  traffic only when NOTHING is left outside the notices, or only boilerplate: whitespace, punctuation, emoji,
  but never a ``?``. Any remaining word is a possible human request, so mixed prompts lean to human and recall
  (a pasted notice plus a question, a quoted ``<task-notification>``, a mention of "ping"). Only CLOSED
  elements are notices (body at most ``BODY_CAP`` chars; an unclosed tag is ordinary text, except a bare
  ``<subagent ...>`` opening tag), and every shape is length-bounded so the scan stays linear on any prompt.
* **agent messages are notices only when they are status**: a ``<subagent>`` / ``<agent-message>`` /
  ``<teammate-message>`` / ``<cross-session-message>`` element is skipped when its body is a JSON object whose
  ``type`` is ``idle_notification``, ``shutdown_*`` or ``*_response``, or when the body is under
  ``STATUS_MAX_CHARS`` characters after stripping the wrapper. A longer prose body is a coordinator's task
  brief: legitimate demand, it recalls. ``<task-notification>`` is a notice unless its body asks a ``?``
  (a human question inside the element). The harness lead-in "Another Claude session sent a message:" and the
  trailer paragraph "This came from another Claude session ..." are wrapper boilerplate: dropped from the
  residue, never a notice on their own, so a wrapped status is skipped and a wrapped brief recalls.
* **harness echoes**: ``[Request interrupted by user...]`` and the slash-command echo elements
  ``<command-name>``, ``<command-message>``, ``<local-command-stdout>`` follow the same residue rule
  (``<command-args>`` is the user's own text and is not one).
* **internal prompt**: the prompt starts with the full distinctive opening phrase of one of our own LLM pipeline
  prompts (:data:`INTERNAL_PROCESS_PATTERNS`, pinned to the real prompt constants by
  ``tests/test_machine_traffic.py``; :data:`EXTERNAL_PROMPT_HEADS` are templates of pipelines outside this repo) even when the process did not inherit the env mark. Anchored at the start:
  a human prompt that merely resembles one, or quotes it mid-sentence, still recalls.

Rollback per adapter: ``MEMORYMASTER_RECALL_MACHINE_FILTER_<ADAPTER>=0`` (``CLAUDE``, ``CODEX``, ``OPENCODE``,
``AGY``) turns both gates off for that adapter only, including the Codex adapter's pre-stdin internal check.

Standard library only (the hook process imports it on every prompt); never raises; logs the category of each
skip, never the prompt text.
"""

from __future__ import annotations

import json
import os
import re
from bisect import bisect_left
from typing import Mapping, NamedTuple

INTERNAL_ENVS = ("MEMORYMASTER_INTERNAL_RUN", "MEMORYMASTER_INTERNAL_CODEX")
FILTER_ENV_PREFIX = "MEMORYMASTER_RECALL_MACHINE_FILTER_"
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


BODY_CAP = 20000  # an element body longer than this is not a notice
STATUS_MAX_CHARS = 200  # an agent message shorter than this is a status notice, a longer prose body is a brief
_STATUS_TYPE = re.compile(r"idle_notification|shutdown_\w+|\w+_response")
_AGENT_TAGS = ("subagent", "agent-message", "teammate-message", "cross-session-message")


class Notice(NamedTuple):
    start: str  # prefix regex: how the metrics script classifies a stored query head
    whole: str  # shape regex of ONE single-line notice, cut out of the prompt; "" for element notices (_ELEMENTS)


# THE one list of transport notices (the harness/orchestrator pushing a notice into a session). Every regex is
# length-bounded: no unbounded scan from a repeated, unclosed prefix.
TRANSPORT_NOTICES = (
    # Orca: "You have N orchestration message(s). Run `orca orchestration check --run run_<id>`."
    Notice(r"You have \d+ orchestration message",
           r"You have \d+ orchestration messages?(?:\(s\))?\. Run `[^`\n]{1,300}`\.?"),
    # Orca terminal wrapper: only the header line "---- From: TERM_x ----" (box-drawing or hyphen rules).
    Notice(r"[─-]{3,} From: ", r"^[ \t]*[─-]{3,} From: [^\n]{1,80}?[─-]{3,}[ \t]*"),
    # Harness envelope: header line, then a body that ends with the task-notification element (an element).
    Notice(r"\[SYSTEM NOTIFICATION", ""),
    # A bare ficha ping tag; a ping with a body sentence is addressed to the agent and recalls.
    Notice(r"\[ficha-ping", r"\[ficha-ping[^\]\n]{0,200}\]"),
    Notice(r"<task-notification", ""),
    # Agent-to-agent wrappers: closed elements (status only, see is_status_body) and a bare <subagent> opening tag.
    Notice(r"<(?:subagent|agent-message|teammate-message|cross-session-message)", r"<subagent\b[^>]{0,300}>"),
    # Harness echoes: an interrupt marker and slash-command echo elements (<command-args> is the user's own text).
    Notice(r"\[Request interrupted by user", r"\[Request interrupted by user[^\]\n]{0,40}\]"),
    Notice(r"<(?:command-name|command-message|local-command-stdout)", ""),
    # The harness lead-in/trailer around a cross-session message are wrappers, see _WRAPPERS.
    Notice(r"Another Claude session sent a message:", ""),
)
TRANSPORT_NOTICE_PATTERNS = tuple(re.compile(n.start) for n in TRANSPORT_NOTICES)
_SHAPES = tuple(re.compile(n.whole, re.MULTILINE) for n in TRANSPORT_NOTICES if n.whole)
# (opening-tag regex, closing literal, body cap, status only, a "?" in the body makes it a human question)
_ELEMENTS = (
    (re.compile(r"\[SYSTEM NOTIFICATION[^\]\n]{0,200}\]"), "</task-notification>", 2000, False, False),
    (re.compile(r"<task-notification\b[^>]{0,300}>"), "</task-notification>", BODY_CAP, False, True),
) + tuple((re.compile(rf"<{tag}\b[^>]{{0,300}}>"), f"</{tag}>", BODY_CAP, True, False) for tag in _AGENT_TAGS) + tuple(
    (re.compile(rf"<{tag}\b[^>]{{0,300}}>"), f"</{tag}>", BODY_CAP, False, False)
    for tag in ("command-name", "command-message", "local-command-stdout"))
# Wrapper boilerplate of a cross-session message: dropped from the residue, but never a notice by itself.
_WRAPPERS = (
    re.compile(r"^[ \t]*Another Claude session sent a message:[ \t]*", re.MULTILINE),
    re.compile(r"This came from another Claude session(?:[^\n]|\n(?!\n)){0,1000}"),
)

# Full opening phrases of our own LLM pipeline prompts, matched at the prompt start. Each is pinned to the real
# prompt constant in tests/test_machine_traffic.py (HEAD_SOURCES); scripts/delivery_metrics.py reads this list.
# Every phrase stays under its HEAD_CHARS (96) window.
INTERNAL_PROMPT_HEADS = (
    "Extract at most five candidates that will change future work.",           # dreaming/providers.py
    "Extract at most five stable facts, decisions, preferences, profiles, or",  # dreaming/providers.py (2 prompts)
    "Extract at most 3 non-obvious bug causes, decisions, gotchas, or constraints.",  # surfaces/session_end_ingest.py
    "You are a memory curator. Extract max 3 non-obvious learnings.",           # hooks/memorymaster-auto-ingest.py
    "You are a memory curator for an AI coding agent system.",                  # govern/llm_steward.py, compact_summaries.py
    "You are a memory quality evaluator. Two claims contradict each other.",    # govern/auto_resolver.py
    "You are preserving working context that a conversation compaction is about to destroy.",  # capture/precompact_residual.py
    "You are a bounded reviewer of recurring agent workflow evidence.",         # knowledge/skills.py
    "You are a knowledge curator. Given a batch of claims from a coding project",  # knowledge/vault_curator.py
    "You are a knowledge base auditor. For each potential contradiction",       # knowledge/vault_linter.py
    "You are a technical writer creating a wiki article about a software project.",  # knowledge/wiki_engine.py
    "You are a wiki editor. Review this article and rate it 1-10:",             # knowledge/wiki_engine.py
    "You judge whether stored memory claims are relevant to a user's message",  # scripts/eval_index_vs_similarity.py
    "You are auditing the long-term memory store of a coding-agent system",     # scripts/judge_claim_value.py
    "You are a relevance judge. Given a USER QUERY and a list of CANDIDATE memory claims",  # scripts/label_prompts_with_judge.py
)
# Templates of pipelines that live OUTSIDE this repo (workflow/fleet tooling), seen in live usage_feedback: no source
# file to pin, so tests/test_machine_traffic.py lists them as external. Still full phrases.
EXTERNAL_PROMPT_HEADS = (
    "You group failures from AI coding-agent sessions",
    "Sos el curador FMX (Haiku)",
    "You decide what to do about repeated agent failures",
)
INTERNAL_PROCESS_PATTERNS = tuple(re.compile(re.escape(head)) for head in INTERNAL_PROMPT_HEADS + EXTERNAL_PROMPT_HEADS) + (
    re.compile(r"Compare the \d+ candidates with governed current claims\."),  # dreaming/providers.py consolidation
)
_BOILERPLATE = re.compile(r"[^\w?¿]*")  # nothing but punctuation/emoji; a "?" is a human asking


def internal_run(environ: Mapping[str, str] = os.environ) -> bool:
    """Whether this process was spawned by one of our own pipelines."""
    return any(environ.get(name, "").strip().lower() in _TRUE for name in INTERNAL_ENVS)


def filter_enabled(adapter: str, environ: Mapping[str, str] = os.environ) -> bool:
    return environ.get(FILTER_ENV_PREFIX + adapter.upper(), "").strip().lower() not in _FALSE


def is_status_body(body: str) -> bool:
    """Whether an agent-message body is a status notice (JSON idle/shutdown/response type, or short text)."""
    if len(body) > 2000:
        return False
    body = body.strip()
    if len(body) < STATUS_MAX_CHARS:
        return True
    if body.startswith("{"):
        try:
            value = json.loads(body)
        except ValueError:
            return False
        return isinstance(value, dict) and isinstance(value.get("type"), str) and bool(_STATUS_TYPE.fullmatch(value["type"]))
    return False


def _element_spans(text: str) -> list[tuple[int, int]]:
    """Spans of CLOSED notice elements. One pass per closing literal (bisect), so linear in the prompt size."""
    spans: list[tuple[int, int]] = []
    closes: dict[str, list[int]] = {}
    for opening, close, cap, status_only, question_is_human in _ELEMENTS:
        covered = -1
        for match in opening.finditer(text):
            if match.start() < covered:
                continue
            if close not in closes:
                closes[close] = [m.start() for m in re.finditer(re.escape(close), text)]
            positions = closes[close]
            index = bisect_left(positions, match.end())
            if index == len(positions) or positions[index] - match.end() > cap:
                continue
            if status_only and (positions[index] - match.end() > 2000
                                or not is_status_body(text[match.end():positions[index]])):
                continue
            if question_is_human and text.find("?", match.end(), positions[index]) != -1:
                continue  # a question inside a notice element is a human asking, not a machine summary
            covered = positions[index] + len(close)
            spans.append((match.start(), covered))
    return spans


def is_transport_notice(prompt: object) -> bool:
    """Whether the prompt is only transport notices (see the module docstring for the mixed-prompt rule)."""
    if not isinstance(prompt, str):
        return False
    spans = _element_spans(prompt)
    for shape in _SHAPES:
        spans.extend(m.span() for m in shape.finditer(prompt))
    if not spans:
        return False
    for wrapper in _WRAPPERS:
        spans.extend(m.span() for m in wrapper.finditer(prompt))
    rest, end = [], 0
    for start, stop in sorted(spans):
        if start > end:
            rest.append(prompt[end:start])
        end = max(end, stop)
    rest.append(prompt[end:])
    return _BOILERPLATE.fullmatch(re.sub(r"\s+", "", "".join(rest))) is not None


def is_internal_prompt(prompt: object) -> bool:
    """Whether the prompt starts with the head of one of our own LLM pipeline prompts."""
    return isinstance(prompt, str) and any(p.match(prompt.lstrip()) for p in INTERNAL_PROCESS_PATTERNS)


def machine_skip(
    prompt: object, adapter: str, *, session: str = "", environ: Mapping[str, str] = os.environ,
) -> str | None:
    """Category (``internal_run`` / ``internal_prompt`` / ``transport_notice``) when this prompt must not reach the ranker, else None.

    Logs ``hook=recall event=skip reason=machine-traffic category=... adapter=...`` (no prompt text).
    """
    try:
        if not filter_enabled(adapter, environ):
            return None
        category = ("internal_run" if internal_run(environ) else "internal_prompt" if is_internal_prompt(prompt)
                    else "transport_notice" if is_transport_notice(prompt) else None)
        if category:
            from memorymaster.core.hook_log import log_hook

            log_hook("recall", "skip", session=session, reason="machine-traffic", category=category, adapter=adapter)
        return category
    except Exception:
        return None
