"""PreCompact residual: keep what a context compaction is about to destroy.

At PreCompact the hook reads the turns that will be summarized, asks the
configured LLM provider for a bounded residual (open decisions, rejected
options with why, unverified assumptions, in-flight card ids, the next intended
step), stores it for one-shot re-injection (``memorymaster.recall.residual``)
and ingests each item as a ``candidate`` claim with
``source_agent="precompact-residual"``. That author is in the non-auto-promotable
set (``core.promotion_policy``), so nothing here is ever confirmed automatically.

A delivered residual is kept (``recall.residual.consume`` renames it) so the next capture can offer the
still-open items back to the extractor: the harness summary and the later turns never contain it, so an item
that lives only there would otherwise die at the second compaction.

Every path is best effort: a failure here must never block or break a compaction.
"""
from __future__ import annotations

import difflib
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
CARRY_HEADER = "previously open (from the last compaction):"
CARRY_CHARS = 1500
CARRY_MIN_PREFIX = 30  # a model that clips a verbatim copy still maps back when the clip keeps this much
CARRY_NEAR_RATIO = 0.9

PREDICATES = {
    "decision": ("decision", "open_decision"),
    "rejected": ("decision", "rejected_option"),
    "assumption": ("fact", "unverified_assumption"),
    "card": ("fact", "in_flight_cards"),
    "next_step": ("fact", "next_step"),
}

# Output budget. The shared provider client caps answers at 500 tokens on the Google, OpenAI and
# Anthropic paths (1500 on Ollama); a truncated JSON object parses to nothing, so the prompt asks
# for a worst case that fits the smallest cap: 8 items x 120 chars + ~200 chars of JSON scaffolding
# is ~1160 chars, about 390 tokens at a pessimistic 3 chars per token.
PROMPT_MAX_ITEMS = 8
PROMPT_ITEM_CHARS = 120
PROVIDER_MIN_OUTPUT_TOKENS = 500

PROMPT = f"""You are preserving working context that a conversation compaction is about to destroy.
Read the conversation excerpt and return ONE JSON object with exactly these keys:
{{"decisions": ["decision still open or just made, one short line"],
 "rejected": [{{"option": "what was rejected", "why": "reason, short"}}],
 "assumptions": ["assumption nobody verified yet"],
 "cards": ["T-1234"],
 "next_step": "the single next intended action"}}
Rules: at most {PROMPT_MAX_ITEMS} items in total, each under {PROMPT_ITEM_CHARS} characters, terse. Only facts stated in the excerpt.
cards are task ids written like T-NNNN that are still in progress. Use [] or "" when a key has nothing.
Lines under "previously open" were open at the last compaction: keep one only if nothing in the conversation shows it closed, copied VERBATIM under the same key (a rejected one as one plain string); otherwise leave it out.
Never include credentials, tokens, keys, private addresses, personal paths, or code. Output JSON only."""

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


def enabled() -> bool:
    return os.environ.get(ENV_ENABLED, "").strip().lower() not in ("", "0", "false", "no", "off")


def _message_text(entry: dict, limit: int = MESSAGE_CHARS) -> tuple[str, str]:
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
    if len(text) > limit:  # decisions tend to sit at the end of a long answer
        text = text[:500] + " … " + text[-(limit - 505):]
    return str(role), text


def _read_turns(transcript_path: str) -> tuple[list[str], str]:
    """Turns after the last compaction boundary and the previous compact summary; ``([], "")`` when unreadable."""
    path = Path(os.path.expanduser(str(transcript_path or "")))
    if not path.is_file():
        return [], ""
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
                if entry.get("isCompactSummary"):  # judged with its own limit, never the per-message one
                    previous_summary = _message_text(entry, PREVIOUS_SUMMARY_CHARS)[1]
                    continue
                role, text = _message_text(entry)
                if not text:
                    continue
                turns.append(f"{role}: {text}")
    except OSError:
        return [], ""
    return turns, previous_summary


def _carry_block(carried: list[dict], room: int) -> tuple[str, list[dict]]:
    """The carried items that fit ``room`` as one line each, and which items made it."""
    if not carried or room <= len(CARRY_HEADER) + 1:
        return "", []
    lines: list[str] = []
    offered: list[dict] = []
    used = len(CARRY_HEADER) + 1
    for item in carried:
        line = f"- {item['kind']}: {item['text']}"
        if used + len(line) + 1 <= room:
            lines.append(line)
            offered.append(item)
            used += len(line) + 1
    return (CARRY_HEADER + "\n" + "\n".join(lines) + "\n", offered) if offered else ("", [])


def _newest_turns(turns: list[str], budget: int) -> tuple[list[str], int]:
    kept: list[str] = []
    used = 0
    for turn in reversed(turns):
        if used + len(turn) > budget:
            break
        kept.append(turn)
        used += len(turn) + 1
    return kept, used


def _assemble(
    turns: list[str], previous_summary: str, budget: int, carried: list[dict] | None = None
) -> tuple[str, str, list[dict]]:
    """``(excerpt, text of the turns kept, carried items offered)``; the excerpt is ``""`` without a turn."""
    if not turns:  # nothing happened after the boundary: the old summary alone is not worth a call
        return "", "", []
    # Carried items are reserved first (at most a fifth of the budget): a full excerpt is the normal case, and
    # a carry that only got leftovers would never be offered. The previous summary only gets what is left.
    carry, offered = _carry_block(carried or [], min(CARRY_CHARS, budget // 5))
    kept, used = _newest_turns(turns, budget - len(carry))
    if not kept and carry:  # the newest turn wins over the carry: drop the carry, never the turn
        carry, offered = "", []
        kept, used = _newest_turns(turns, budget)
    if not kept:  # not even the newest turn fits: the old summary alone is not worth a call
        return "", "", []
    label = "previous summary: "
    room = min(PREVIOUS_SUMMARY_CHARS, budget // 5, budget - used - len(carry) - len(label) - 1)
    prefix = f"{label}{previous_summary[:room]}\n" if previous_summary and room > 0 else ""
    body = "\n".join(reversed(kept))
    return carry + prefix + body, body, offered


def slice_turns(transcript_path: str, max_chars: int | None = None) -> str:
    """Text of the turns after the last compaction boundary, newest kept when over budget.

    Tool results and sub-agent turns are skipped. The previous compaction's
    summary, when present, leads the excerpt so earlier facts can be carried on.
    """
    turns, previous_summary = _read_turns(transcript_path)
    return _assemble(turns, previous_summary, max_chars if max_chars is not None else _input_chars())[0]


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


def _scrub_raw(parsed: Any) -> dict:
    """Remove sensitive strings from the raw parsed answer BEFORE it is clipped.

    Clipping to the item cap can cut a token in half so the filter no longer matches it; the
    check therefore runs on the full values. A sensitive value drops its whole list entry (or key).
    """

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return None if _sensitive(value) else value
        if isinstance(value, list):
            return [kept for kept in (clean(v) for v in value) if kept is not None]
        if isinstance(value, dict):
            cleaned = {k: clean(v) for k, v in value.items()}
            return None if any(v is None for v in cleaned.values()) else cleaned
        return value

    top = next((entry for entry in parsed if isinstance(entry, dict)), {}) if isinstance(parsed, list) else parsed
    if not isinstance(top, dict):
        return {}
    return {key: cleaned for key, value in top.items() if (cleaned := clean(value)) is not None}


def drop_sensitive(items: list[dict[str, str]]) -> list[dict[str, str]]:
    return [item for item in items if not _sensitive(item["text"])]


def extract_residual(
    excerpt: str,
    *,
    llm: Callable[[str, str], str] | None = None,
    config: dict[str, str] | None = None,
    stats: dict[str, int] | None = None,
) -> list[dict[str, str]]:
    """One bounded LLM call over the excerpt; ``[]`` on any failure or empty answer."""
    if not excerpt.strip():
        return []
    from memorymaster.core.llm_provider import call_llm, parse_json_response, use_call_scoped_env
    from memorymaster.core.provider_egress import export_lines  # fail-closed: no filter, no call

    config = config or provider_config()
    call = llm or call_llm
    overrides: dict[str, str | None] = {}
    if config.get("forced"):
        overrides["MEMORYMASTER_LLM_PROVIDER"] = config["provider"]
    if config.get("model"):
        overrides["MEMORYMASTER_LLM_MODEL"] = config["model"]
    elif config.get("forced"):
        overrides["MEMORYMASTER_LLM_MODEL"] = None  # the machine-wide model belongs to the other provider
    safe_excerpt, withheld = export_lines(excerpt)  # nothing credential-grade leaves toward a provider
    if stats is not None:
        stats["egress_withheld"] = withheld
    if not safe_excerpt:  # every line withheld: nothing left worth a provider call
        return []
    with use_call_scoped_env(overrides):
        answer = call(PROMPT, safe_excerpt)
    if stats is not None:
        stats["answered"] = 1  # the provider call returned: an empty answer still settles the carried items
    if not answer:
        return []
    return drop_sensitive(residual_state.bound_residual(_scrub_raw(parse_json_response(answer))))


def _norm(text: str) -> str:
    return " ".join(text.lower().split()).rstrip("….").strip()


def _same_item(item: dict, carried: dict) -> bool:
    """Is a model item a copy of a carried one: equal, a clipped copy, or near-identical (cards: the id)."""
    if (item["kind"] == "card") != (carried["kind"] == "card"):
        return False
    a, b = _norm(item["text"]), _norm(carried["text"])
    if item["kind"] == "card" or a == b:
        return a == b
    short, long_ = sorted((a, b), key=len)
    if len(short) >= CARRY_MIN_PREFIX and long_.startswith(short):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= CARRY_NEAR_RATIO


def _restated(item: dict, turns_norm: str) -> bool:
    if item["kind"] == "card":
        return re.search(rf"\b{re.escape(item['text'].lower())}\b", turns_norm) is not None
    return _norm(item["text"]) in turns_norm


def carry_candidates(carried: list[dict]) -> list[dict]:
    """Previous items worth offering again: below the carry limit and clean for the provider."""
    return drop_sensitive([c for c in carried if c.get("carried", 0) < residual_state.MAX_CARRIES])


def reconcile_carried(items: list[dict], offered: list[dict], turns_text: str) -> list[dict]:
    """Map every model item that is a copy of an offered one back to the offered item's exact kind and text.

    The ingest key is built from kind and text, so a paraphrase would mint a new claim at every
    compaction. A copy keeps a ``carried`` counter (one more compaction crossed) unless the
    conversation itself still shows it, which resets it; an item over the limit is not offered again.
    """
    if not offered:
        return items
    turns_norm = _norm(turns_text)
    pool = list(offered)
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        match = next((c for c in pool if c["kind"] == item["kind"] and _same_item(item, c)), None)
        match = match or next((c for c in pool if _same_item(item, c)), None)
        if match is None:
            kept = item
        else:
            pool.remove(match)
            count = 0 if _restated(match, turns_norm) else match.get("carried", 0) + 1
            kept = {"kind": match["kind"], "text": match["text"], **({"carried": count} if count else {})}
        key = (kept["kind"], kept["text"].lower())
        if key not in seen:
            seen.add(key)
            out.append(kept)
    return residual_state._fit(out)


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
    residual_state.sweep_carried(directory=state_directory)
    carried = carry_candidates(residual_state.previous_items(session_id, directory=state_directory))
    turns, previous_summary = _read_turns(str(data.get("transcript_path") or ""))
    excerpt, turns_text, offered = _assemble(turns, previous_summary, _input_chars(), carried)
    outcome["excerpt_chars"] = len(excerpt)
    outcome["carry_offered"] = len(offered)
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
        egress: dict[str, int] = {}
        items = extract_residual(excerpt, llm=llm, config=config, stats=egress)
        outcome["egress_withheld"] = egress.get("egress_withheld", 0)
        if egress.get("answered"):
            items = reconcile_carried(items, offered, turns_text)
    except Exception as exc:  # swallow-ok: provider outage or quota must never block a compaction; recorded below
        outcome["skipped"] = "llm-error"
        outcome["error"] = type(exc).__name__
        if reservation is not None:
            ledger.finish_llm(reservation, input_bytes=len(excerpt), output_bytes=0, outcome="error")
        return outcome
    if reservation is not None:
        ledger.finish_llm(reservation, input_bytes=len(excerpt), output_bytes=sum(len(i["text"]) for i in items), outcome="ok")
    outcome["items"] = len(items)
    outcome["carried"] = sum(1 for i in items if i.get("carried"))
    if egress.get("answered"):  # the carried items were judged: re-saved below with new counters, or let go
        residual_state.retire_carried(session_id, directory=state_directory)
    if not items:
        outcome["skipped"] = "empty-residual"
        return outcome
    try:
        saved = residual_state.save_residual(
            session_id,
            items,
            meta={"provider": config["provider"], "model": config["model"], "trigger": str(data.get("trigger") or "")},
            directory=state_directory,
        )
    except Exception as exc:  # swallow-ok: a marker that cannot be written must not abort the claim ingestion below
        saved = None
        outcome["save_error"] = type(exc).__name__
    outcome["saved"] = saved is not None
    try:
        # A carried copy is already a claim from the capture that found it: no second write, no revival churn.
        fresh = [i for i in items if not i.get("carried")]
        outcome["ingested"] = ingest_residual(fresh, db_path=db_path, session_id=session_id, cwd=cwd, spool=spool)
    except Exception as exc:  # swallow-ok: the residual file already protects the session; ingest is the durable extra
        outcome["ingest_error"] = type(exc).__name__
    return outcome
