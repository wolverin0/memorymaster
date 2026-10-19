"""PreCompact residual: keep what a context compaction is about to destroy.

At PreCompact the hook reads the turns that will be summarized, asks the
configured LLM provider for a bounded residual (open decisions, rejected
options with why, unverified assumptions, in-flight card ids, the next intended
step), stores it for one-shot re-injection (``memorymaster.recall.residual``)
and ingests each item as a ``candidate`` claim with
``source_agent="precompact-residual"``. That author is in the non-auto-promotable
set (``core.promotion_policy``), so nothing here is ever confirmed automatically.

Every path is best effort: a failure here must never block or break a compaction.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable

from memorymaster.recall import residual as residual_state

SOURCE_AGENT = "precompact-residual"
ENV_ENABLED = "MEMORYMASTER_PRECOMPACT_RESIDUAL"
ENV_MODEL = "MEMORYMASTER_PRECOMPACT_RESIDUAL_MODEL"
ENV_PROVIDER = "MEMORYMASTER_PRECOMPACT_RESIDUAL_PROVIDER"
ENV_INPUT_CHARS = "MEMORYMASTER_PRECOMPACT_RESIDUAL_INPUT_CHARS"
DEFAULT_MODEL = "claude-sonnet-5-5"
ANTHROPIC_PROVIDERS = frozenset({"anthropic", "claude", "claude_cli", "claude-cli"})
DEFAULT_INPUT_CHARS = 30_000
MESSAGE_CHARS = 1500
PREVIOUS_SUMMARY_CHARS = 2500

PREDICATES = {
    "decision": ("decision", "open_decision"),
    "rejected": ("decision", "rejected_option"),
    "assumption": ("fact", "unverified_assumption"),
    "card": ("fact", "in_flight_cards"),
    "next_step": ("fact", "next_step"),
}

PROMPT = """You are preserving working context that a conversation compaction is about to destroy.
Read the conversation excerpt and return ONE JSON object with exactly these keys:
{"decisions": ["decision still open or just made, one short line"],
 "rejected": [{"option": "what was rejected", "why": "reason, short"}],
 "assumptions": ["assumption nobody verified yet"],
 "cards": ["T-1234"],
 "next_step": "the single next intended action"}
Rules: at most 10 items in total, each under 200 characters, terse. Only facts stated in the excerpt.
cards are task ids written like T-NNNN that are still in progress. Use [] or "" when a key has nothing.
Never include credentials, tokens, keys, private addresses, personal paths, or code. Output JSON only."""

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


def enabled() -> bool:
    return os.environ.get(ENV_ENABLED, "").strip().lower() not in ("", "0", "false", "no", "off")


def _message_text(entry: dict) -> tuple[str, str]:
    message = entry.get("message") if isinstance(entry.get("message"), dict) else entry
    role = message.get("role") or entry.get("type")
    if role not in ("user", "assistant"):
        return "", ""
    content = message.get("content", "")
    if isinstance(content, list):
        content = " ".join(
            block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
        )
    if not isinstance(content, str):
        return "", ""
    text = _REMINDER_RE.sub("", content).strip()
    if not text or "<command-message>" in text:
        return "", ""
    if len(text) > MESSAGE_CHARS:  # decisions tend to sit at the end of a long answer
        text = text[:500] + " … " + text[-(MESSAGE_CHARS - 505):]
    return str(role), text


def slice_turns(transcript_path: str, max_chars: int | None = None) -> str:
    """Text of the turns after the last compaction boundary, newest kept when over budget.

    Tool results and sub-agent turns are skipped. The previous compaction's
    summary, when present, leads the excerpt so earlier facts can be carried on.
    """
    budget = max_chars if max_chars is not None else _input_chars()
    path = Path(os.path.expanduser(str(transcript_path or "")))
    if not path.is_file():
        return ""
    turns: list[str] = []
    previous_summary = ""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict) or entry.get("isSidechain") is True:
                    continue
                if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
                    turns, previous_summary = [], ""
                    continue
                role, text = _message_text(entry)
                if not text:
                    continue
                if entry.get("isCompactSummary"):
                    previous_summary = text[:PREVIOUS_SUMMARY_CHARS]
                    continue
                turns.append(f"{role}: {text}")
    except OSError:
        return ""
    kept: list[str] = []
    used = len(previous_summary)
    for turn in reversed(turns):
        if used + len(turn) > budget:
            break
        kept.append(turn)
        used += len(turn) + 1
    excerpt = "\n".join(reversed(kept))
    return (f"previous summary: {previous_summary}\n" if previous_summary else "") + excerpt


def _input_chars() -> int:
    try:
        return max(2000, int(os.environ.get(ENV_INPUT_CHARS, DEFAULT_INPUT_CHARS)))
    except ValueError:
        return DEFAULT_INPUT_CHARS


def provider_config(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Which provider/model the residual call uses, and why.

    A Claude provider gets a Sonnet-class model. Any other configured provider is
    used as configured and the note says so. The hook only changes provider when
    ``MEMORYMASTER_PRECOMPACT_RESIDUAL_PROVIDER`` names one, which scopes the choice
    to this call instead of changing the machine-wide provider every hook shares.
    """
    env = os.environ if environ is None else environ
    forced = (env.get(ENV_PROVIDER) or "").strip().lower()
    provider = forced or (env.get("MEMORYMASTER_LLM_PROVIDER") or "google").strip().lower()
    override = (env.get(ENV_MODEL) or "").strip()
    if provider in ANTHROPIC_PROVIDERS:
        return {
            "provider": provider,
            "model": override or DEFAULT_MODEL,
            "forced": bool(forced),
            "note": "sonnet-class model on a Claude provider",
        }
    return {
        "provider": provider,
        "model": override,
        "forced": bool(forced),
        "note": f"provider {provider} is not Anthropic; used as configured (no Sonnet-class model available through it)",
    }


def _sensitive(text: str) -> bool:
    try:
        from memorymaster.core.security import redact_text
    except ImportError:
        return True  # fail closed: an unreviewable text is never stored or injected
    return bool(redact_text(text)[1])


def drop_sensitive(items: list[dict[str, str]]) -> list[dict[str, str]]:
    return [item for item in items if not _sensitive(item["text"])]


def extract_residual(
    excerpt: str,
    *,
    llm: Callable[[str, str], str] | None = None,
    config: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """One bounded LLM call over the excerpt; ``[]`` on any failure or empty answer."""
    if not excerpt.strip():
        return []
    from memorymaster.core.llm_provider import call_llm, parse_json_response, use_call_scoped_env
    from memorymaster.core.security import redact_text

    config = config or provider_config()
    call = llm or call_llm
    overrides: dict[str, str | None] = {}
    if config.get("forced"):
        overrides["MEMORYMASTER_LLM_PROVIDER"] = config["provider"]
    if config.get("model"):
        overrides["MEMORYMASTER_LLM_MODEL"] = config["model"]
    elif config.get("forced"):
        overrides["MEMORYMASTER_LLM_MODEL"] = None  # the machine-wide model belongs to the other provider
    safe_excerpt = redact_text(excerpt)[0]  # secrets never leave the machine toward a provider
    with use_call_scoped_env(overrides):
        answer = call(PROMPT, safe_excerpt)
    if not answer:
        return []
    return drop_sensitive(residual_state.bound_residual(parse_json_response(answer)))


def _claim_args(item: dict[str, str], *, scope: str, session_id: str, subject: str) -> dict[str, Any]:
    claim_type, predicate = PREDICATES[item["kind"]]
    text = item["text"]
    if item["kind"] == "card":
        text = "In-flight card: " + text
    digest = hashlib.sha256(f"{session_id}|{item['kind']}|{item['text'].lower()}".encode()).hexdigest()[:16]
    return {
        "text": text,
        "claim_type": claim_type,
        "subject": subject,
        "predicate": predicate,
        "scope": scope,
        "confidence": 0.5,
        "source_agent": SOURCE_AGENT,
        "idempotency_key": f"precompact-residual-{digest}",
        "citation": {"source": "claude-session", "locator": session_id, "excerpt": text[:200]},
    }


def ingest_residual(
    items: list[dict[str, str]],
    *,
    db_path: str,
    session_id: str,
    cwd: str,
    spool: bool = False,
    workspace_root: Path | None = None,
) -> int:
    """Ingest each item as a candidate claim; returns the number accepted.

    Goes through ``MemoryService.ingest`` (sensitivity sanitize, dedup, citation
    check) or, under WAL discipline, the spool the steward drain replays through
    the same service. Never sets a status: claims enter as ``candidate``.
    """
    from memorymaster.core.scope_utils import scope_from_cwd

    scope = scope_from_cwd(cwd)
    if scope == "global":  # no project resolved from cwd: a residual is project working state, never global
        return 0
    subject = scope.split(":", 1)[-1]
    batch_id = "precompact-" + uuid.uuid4().hex[:16]
    batch_max = residual_state.MAX_ITEMS
    accepted = 0
    if spool:
        from memorymaster.core import spool as spool_module

        for item in items:
            args = _claim_args(item, scope=scope, session_id=session_id, subject=subject)
            citation = args.pop("citation")
            key = args.pop("idempotency_key")
            try:
                spool_module.append(
                    db_path,
                    "ingest",
                    {**args, "intake_batch_id": batch_id, "intake_batch_max": batch_max, "citations": [citation]},
                    idempotency_key=key,
                )
                accepted += 1
            except Exception:  # swallow-ok: one bad item must not drop the rest of the residual
                continue
        return accepted
    if not os.path.exists(db_path):
        return 0
    from memorymaster.core.models import CitationInput
    from memorymaster.core.service import MemoryService

    svc = MemoryService(db_path, workspace_root=workspace_root or Path(cwd or "."))
    for item in items:
        args = _claim_args(item, scope=scope, session_id=session_id, subject=subject)
        citation = CitationInput(**args.pop("citation"))
        try:
            svc.ingest(citations=[citation], intake_batch_id=batch_id, intake_batch_max=batch_max, **args)
            accepted += 1
        except Exception:  # swallow-ok: one rejected item (filter, dedup, batch fence) must not drop the rest
            continue
    return accepted


def capture_residual(
    data: dict,
    *,
    db_path: str,
    llm: Callable[[str, str], str] | None = None,
    spool: bool = False,
    state_directory: Path | None = None,
    ledger: Any = None,
) -> dict[str, Any]:
    """Whole PreCompact capture for one hook payload; returns a small outcome record."""
    session_id = residual_state.safe_session(data.get("session_id"))
    from memorymaster.core.scope_utils import cwd_from_transcript

    cwd = str(data.get("cwd") or cwd_from_transcript(data.get("transcript_path")) or "")
    config = provider_config()
    outcome: dict[str, Any] = {"session": session_id, "provider": config["provider"], "note": config["note"], "items": 0, "ingested": 0}
    excerpt = slice_turns(str(data.get("transcript_path") or ""))
    outcome["excerpt_chars"] = len(excerpt)
    if not excerpt:
        outcome["skipped"] = "no-turns"
        return outcome
    reservation = None
    if ledger is not None:
        # Own session key: the residual shares the global/provider daily caps but not the
        # per-session stop-hook allowance, so a busy session can still be compacted safely.
        reservation = ledger.reserve_llm(config["provider"], session_id + ":precompact", "precompact-residual")
        if reservation is None:
            outcome["skipped"] = "capture-budget"
            return outcome
    try:
        items = extract_residual(excerpt, llm=llm, config=config)
    except Exception as exc:  # swallow-ok: provider outage or quota must never block a compaction; recorded below
        outcome["skipped"] = "llm-error"
        outcome["error"] = type(exc).__name__
        if reservation is not None:
            ledger.finish_llm(reservation, input_bytes=len(excerpt), output_bytes=0, outcome="error")
        return outcome
    if reservation is not None:
        ledger.finish_llm(reservation, input_bytes=len(excerpt), output_bytes=sum(len(i["text"]) for i in items), outcome="ok")
    outcome["items"] = len(items)
    if not items:
        outcome["skipped"] = "empty-residual"
        return outcome
    residual_state.save_residual(
        session_id,
        items,
        meta={"provider": config["provider"], "model": config["model"], "trigger": str(data.get("trigger") or "")},
        directory=state_directory,
    )
    try:
        outcome["ingested"] = ingest_residual(items, db_path=db_path, session_id=session_id, cwd=cwd, spool=spool)
    except Exception as exc:  # swallow-ok: the residual file already protects the session; ingest is the durable extra
        outcome["ingest_error"] = type(exc).__name__
    return outcome
