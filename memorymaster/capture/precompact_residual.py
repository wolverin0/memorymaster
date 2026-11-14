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
CARRY_NEAR_RATIO = 0.7  # loose on purpose: a rewrite that slips past it is still caught by the grounding rule
GROUND_RATIO = 0.6  # share of an item's content tokens that must appear in the conversation to count as stated there
GROUND_MIN_TOKENS = 3  # below this (or below 20 chars) an item is too short to be "restated" by token overlap

ANSWER_KEYS = frozenset({
    "decisions", "open_decisions", "rejected", "rejected_options", "assumptions", "unverified_assumptions",
    "cards", "in_flight_cards", "next_step", "next",
})

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


def _read_turns(transcript_path: str) -> tuple[list[str], str, int]:
    """Turns after the last boundary, the previous compact summary and how many compactions the file shows."""
    path = Path(os.path.expanduser(str(transcript_path or "")))
    if not path.is_file():
        return [], "", 0
    turns: list[str] = []
    previous_summary = ""
    boundaries = 0
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
                    boundaries += 1
                    continue
                if entry.get("isCompactSummary"):  # judged with its own limit, never the per-message one
                    previous_summary = _message_text(entry, PREVIOUS_SUMMARY_CHARS)[1]
                    continue
                role, text = _message_text(entry)
                if not text:
                    continue
                turns.append(f"{role}: {text}")
    except OSError:
        return [], "", 0
    return turns, previous_summary, boundaries


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
    """``(excerpt, text of the turns kept, carried items offered)``; the excerpt is ``""`` without a turn.

    The turns after the boundary are the only ground a carried item is judged against: neither the carry
    block nor the previous summary (a model that rewrites a carried item would otherwise find it "stated"
    there and mint a new claim every compaction).
    """
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
    return carry + prefix + body, body, offered  # the middle value is what the carry is judged against: the turns only


def slice_turns(transcript_path: str, max_chars: int | None = None) -> str:
    """Text of the turns after the last compaction boundary, newest kept when over budget.

    Tool results and sub-agent turns are skipped. The previous compaction's
    summary, when present, leads the excerpt so earlier facts can be carried on.
    """
    turns, previous_summary, _ = _read_turns(transcript_path)
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
    if not answer:  # a failed call (the shared client returns "") judged nothing
        return []
    parsed = parse_json_response(answer)
    top = next((entry for entry in parsed if isinstance(entry, dict)), {})
    if stats is not None and ANSWER_KEYS & set(top):
        stats["answered"] = 1  # an object with the expected keys, even all empty, is a verdict; {} / an error object / garbage is not
    return drop_sensitive(residual_state.bound_residual(_scrub_raw(parsed)))


def _norm(text: str) -> str:
    return " ".join(text.lower().split()).rstrip("….").strip()


_NEGATION = re.compile(r"\b(?:not|no|never|without|nunca|sin)\b|n't")
_STOPWORDS = frozenset(
    "that this with from have has had will would should could about into over than then when what which "
    "your their there these those been being were does doing done need needs use using used also just only "
    "para como pero esto esta este que con por una uno los las del".split()
)


def _meaning(text: str) -> tuple[list[str], bool]:
    """What a near-identical match must not change: the numbers and whether the line negates."""
    return re.findall(r"\d+(?:[.,]\d+)*", text), _NEGATION.search(text) is not None


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[\w-]{4,}", text.lower()) if t not in _STOPWORDS}


def _same_item(item: dict, carried: dict) -> bool:
    """Is a model item a copy of a carried one: equal, a clipped copy (same kind), or near-identical."""
    if (item["kind"] == "card") != (carried["kind"] == "card"):
        return False
    a, b = _norm(item["text"]), _norm(carried["text"])
    if item["kind"] == "card" or a == b:
        return a == b
    short, long_ = sorted((a, b), key=len)
    if item["kind"] == carried["kind"] and len(short) >= CARRY_MIN_PREFIX and long_.startswith(short):
        return True
    # shortcut: a changed number or an added "not" is a different item and mints a new claim, never a silent merge
    return _meaning(a) == _meaning(b) and difflib.SequenceMatcher(None, a, b).ratio() >= CARRY_NEAR_RATIO


def _stated(item: dict, ground_norm: str, ground_tokens: set[str], *, short_by_substring: bool) -> bool:
    """Do the turns after the boundary (never the carry block or the previous summary) say this item?

    Cards: the exact id. Text: token overlap, and only for an item with enough content; a short one
    ("run tests") is found by a raw substring only when ``short_by_substring`` (grounding a NEW item),
    never to reset a carried item's age (it would match any later turn forever).
    """
    if item["kind"] == "card":
        return re.search(rf"\b{re.escape(item['text'].lower())}\b", ground_norm) is not None
    tokens = _tokens(item["text"])
    if len(tokens) >= GROUND_MIN_TOKENS and len(_norm(item["text"])) >= 20:
        return len(tokens & ground_tokens) / len(tokens) >= GROUND_RATIO
    return short_by_substring and _norm(item["text"]) in ground_norm


def carry_candidates(carried: list[dict]) -> list[dict]:
    """Previous items worth offering again: below the carry limit and clean for the provider."""
    return drop_sensitive([c for c in carried if c.get("carried", 0) < residual_state.MAX_CARRIES])


def reconcile_carried(
    items: list[dict],
    offered: list[dict],
    turns_text: str,
    *,
    ticked: bool = True,
    unoffered: list[dict] | tuple = (),
) -> list[dict]:
    """Keep the lineage of carried items bounded whatever the model does with their text.

    An item of the answer is, in order: a copy of an offered item (equal, clipped or near-identical;
    maps back to the offered kind and text, so the ingest key is unchanged); a REWRITE of an offered
    item (not a copy and not stated in the conversation while offered items of its kind went
    unmatched: it takes that item's place, text included); or genuinely new (stated in the
    turns, or nothing left to attribute it to: ingested as before, age 0). Copies and rewrites
    keep the ``carried`` counter plus one real compaction (``ticked``), reset when the conversation
    states the item itself; variants of one carried item collapse into it. Items that did not fit the
    carried block were never judged: they ride along, aged, while there is room.
    """
    if not offered and not unoffered:
        return items
    ground_norm = _norm(turns_text)
    ground_tokens = _tokens(turns_text)

    def aged(carried: dict) -> dict:
        count = 0 if _stated(carried, ground_norm, ground_tokens, short_by_substring=False) else carried.get("carried", 0) + int(ticked)
        return {"kind": carried["kind"], "text": carried["text"], **({"carried": count} if count else {})}

    matched: list[int | None] = []
    used: set[int] = set()
    for item in items:
        index = next((n for n, c in enumerate(offered) if c["kind"] == item["kind"] and _same_item(item, c)), None)
        if index is None:
            index = next((n for n, c in enumerate(offered) if _same_item(item, c)), None)
        matched.append(index)
        if index is not None:
            used.add(index)
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(entry: dict) -> None:
        key = (entry["kind"], entry["text"].lower())
        if key not in seen:
            seen.add(key)
            out.append(entry)

    for item, index in zip(items, matched):
        if index is None and item["kind"] != "card" and not _stated(item, ground_norm, ground_tokens, short_by_substring=True):
            free = [n for n, c in enumerate(offered) if n not in used and c["kind"] == item["kind"]]
            if free:  # a rewrite of the closest unmatched carried item of its kind
                index = max(free, key=lambda n: difflib.SequenceMatcher(None, _norm(item["text"]), _norm(offered[n]["text"])).ratio())
                used.add(index)
        add(aged(offered[index]) if index is not None else item)
    fitted = residual_state._fit(out)
    for carried in unoffered:
        extra = aged(carried)
        if (extra["kind"], extra["text"].lower()) in seen or len(fitted) >= residual_state.MAX_ITEMS:
            continue
        if len(residual_state.render_block([*fitted, extra])) <= residual_state.MAX_CHARS:
            seen.add((extra["kind"], extra["text"].lower()))
            fitted.append(extra)
    return residual_state._fit(fitted)


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
    previous, previous_boundaries = residual_state.previous_items(session_id, directory=state_directory)
    carried = carry_candidates(previous)
    turns, previous_summary, boundaries = _read_turns(str(data.get("transcript_path") or ""))
    excerpt, turns_text, offered = _assemble(turns, previous_summary, _input_chars(), carried)
    unoffered = [c for c in carried if not any(c is o for o in offered)]  # no room in the block: not judged, not lost
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
            ticked = previous_boundaries is None or boundaries > previous_boundaries
            items = reconcile_carried(items, offered, turns_text, ticked=ticked, unoffered=unoffered)
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
    if not items:
        if egress.get("answered"):  # judged and nothing is open: the carried items are let go
            residual_state.retire_carried(session_id, directory=state_directory)
        outcome["skipped"] = "empty-residual"
        return outcome
    try:
        saved = residual_state.save_residual(
            session_id,
            items,
            meta={"provider": config["provider"], "model": config["model"], "trigger": str(data.get("trigger") or ""), "boundaries": boundaries},
            directory=state_directory,
        )
    except Exception as exc:  # swallow-ok: a marker that cannot be written must not abort the claim ingestion below
        saved = None
        outcome["save_error"] = type(exc).__name__
    outcome["saved"] = saved is not None
    if saved is not None and egress.get("answered"):  # re-saved with new counters: the old copy has served
        residual_state.retire_carried(session_id, directory=state_directory)
    try:
        # A carried copy is already a claim from the capture that found it: no second write, no revival churn.
        fresh = [i for i in items if not i.get("carried")]
        outcome["ingested"] = ingest_residual(fresh, db_path=db_path, session_id=session_id, cwd=cwd, spool=spool)
    except Exception as exc:  # swallow-ok: the residual file already protects the session; ingest is the durable extra
        outcome["ingest_error"] = type(exc).__name__
    return outcome
