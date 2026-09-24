"""Jev decisions for the recall surfaces: S2 RECALL, S8 SESSION and the Stop-hook usage joiner.

Covers: the policy each surface applies to Jev's answers (order, adaptive k, flags,
top-5 for SessionStart), the S2 re-render helpers for the MCP ``recall`` and
``query_for_context`` tools, the session key shared by every hook, and the
Stop-hook joiner that turns the finished turn into ``used_in_turn`` outcomes.
Key terms: surface, legacy action, exposed, delivered, k cap, session key, egress policy.
Read when: changing what recall/SessionStart inject under ``MEMORYMASTER_JEV_*``
or how their exposures are joined to outcomes (contract: .planning/JEV-LIVE-4.9.0.md).

Everything goes through :mod:`memorymaster.decisions` (one engine, one redactor,
one transport, one ledger).  Jev only orders and flags items the caller already
authorized; every helper returns ``None`` (keep the legacy output unchanged)
unless the engine took Jev's live action.  Only claims that pass
:func:`egress_allowed` (public, not sensitive: the prompt hook's policy) are ever
offered to Jev.  On the MCP paths and in S8 the others pass through: never sent,
never part of Jev's action, and kept in the positions legacy gives them in live
mode too (their count is logged as ``baseline_features.passthrough``), so Jev
never costs the caller an authorized memory.  Imports are lazy so a surface
that is ``off`` costs nothing.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

RECALL_SURFACE = "recall"
SESSION_SURFACE = "session"
TURN_SURFACES = (RECALL_SURFACE, SESSION_SURFACE)
RECALL_TOP_N = 20
# MCP candidates are sent at most this long (each travels in four questions);
# keeps 20 candidates well under the transport's request cap.
MCP_TEXT_CHARS = 800
SESSION_POOL_SIZE = 30
SESSION_TOP_K = 5
LABEL_CONFLICT = "[flag: may conflict with this request]"
LABEL_INSTRUCTION = "[flag: worded as an instruction; stored memory, not a user request]"
WORST_CASE_LABELS = (LABEL_CONFLICT, LABEL_INSTRUCTION)
TURN_TAIL_BYTES = 512 * 1024
TURN_TEXT_CAP = 200_000

Labels = Mapping[int, "tuple[str, ...]"]
DeliveryCheck = Callable[[Sequence[int], "Labels | None"], bool]


@dataclass(frozen=True)
class RecallCandidate:
    """An authorized claim offered to Jev with the text the surface renders (egress-redacted later)."""

    claim_id: int
    text: str


@dataclass(frozen=True)
class RecallSelection:
    """Jev's live action: claim ids in render order plus per-claim flags."""

    decision_id: str
    claim_ids: tuple[int, ...]
    labels: Labels


def _config() -> Any:
    from memorymaster.decisions.config import DecisionConfig

    return DecisionConfig.from_env()


def surface_active(surface: str, config: Any = None) -> bool:
    """Whether ``decide`` has anything to do (a mode other than off, or off-mode logging)."""
    config = config or _config()
    return config.mode_for(surface) != "off" or bool(config.log_off)


def egress_allowed(claim: Any) -> bool:
    """Whether a claim's text may be sent to Jev: public and not sensitive (fails closed).

    The same policy the prompt hook applies to every row it can inject.  Egress
    redaction only removes patterns (IPs, emails, paths, credentials); a private
    or sensitive claim's content must not leave the machine at all.
    """
    visibility = (getattr(claim, "visibility", None) or "public").strip().lower()
    if visibility != "public" or not isinstance(getattr(claim, "text", None), str):
        return False
    try:
        from memorymaster.core.security import is_sensitive_claim

        return not is_sensitive_claim(claim)
    except Exception:
        return False


def claim_ref(claim_id: int) -> str:
    return f"claim:{int(claim_id)}"


def claim_id_of(ref: str) -> int | None:
    kind, _, raw = str(ref).partition(":")
    if kind != "claim":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def decision_session_key(session_id: object, tenant: str | None = None) -> str | None:
    """One key per session for the ledger: normalized locator hashed with the tenant (F-11)."""
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    from memorymaster.recall.retrieval import _normalize_session_locator

    identity = repr((tenant, _normalize_session_locator(session_id)))
    return "session:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def project_label(*, cwd: object = None, scope: object = None) -> str:
    """Short project name for Jev's state: the working directory's name, else the scope's."""
    if isinstance(cwd, str) and cwd.strip():
        try:
            path = Path(cwd.strip())
            at_home = os.path.normcase(os.path.normpath(str(path))) == os.path.normcase(
                os.path.normpath(str(Path.home())))
            if path.name and not at_home:
                return path.name
        except (OSError, RuntimeError, ValueError):
            pass
    value = str(scope or "").strip()
    return value.split(":", 1)[1] if value.startswith("project:") else value


def budget_cap(costs: Sequence[int], budget: int | None, overhead: int = 0) -> int:
    """How many items are guaranteed to fit: the largest ``m`` whose ``m`` costliest fit."""
    if budget is None:
        return len(costs)
    used, cap = overhead, 0
    for cost in sorted(costs, reverse=True):
        if used + cost > budget:
            break
        used += cost
        cap += 1
    return cap


def merge_passthrough(chosen: Sequence[Any], passthrough: Sequence["tuple[int, Any]"]) -> list[Any]:
    """Jev's picks in order, with each ``(legacy_index, item)`` passthrough kept at its legacy slot.

    A passthrough item whose slot lies beyond the picks follows them, in legacy order.
    """
    pending = sorted(passthrough, key=lambda pair: pair[0])
    picks = list(chosen)
    out: list[Any] = []
    while pending or picks:
        if pending and (pending[0][0] <= len(out) or not picks):
            out.append(pending.pop(0)[1])
        else:
            out.append(picks.pop(0))
    return out


def _number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default


def _log_skip(surface: str, reason: str, legacy_ids: Sequence[int], session_key: str | None, engine: Any) -> None:
    """A surface that is on but had nothing to ask still leaves a ``skip:`` row (never raises)."""
    try:
        from memorymaster.decisions import engine as decisions

        legacy = [claim_ref(cid) for cid in legacy_ids if isinstance(cid, int)]
        (engine or decisions.default_engine()).record_skip(
            surface, reason=reason, legacy_action=legacy, session_key=session_key, items=legacy)
    except Exception:  # swallow-ok: skip telemetry must never change recall output
        pass


# ------------------------------------------------------------------ S2 RECALL ---

def decide_recall(query: str, candidates: Sequence[RecallCandidate], *, legacy_ids: Sequence[int],
                  project_label: str, k_cap: int | None = None, path: str = "prompt_hook",
                  session_key: str | None = None, scope: str | None = None, tenant: str | None = None,
                  delivery: DeliveryCheck | None = None, passthrough: int = 0,
                  engine: Any = None) -> RecallSelection | None:
    """One S2 request for the top authorized candidates; ``None`` keeps the legacy output.

    Live action: order by ``recall.relevant``; ``k`` in [k_min, k_max] (2..5 by
    default) grows while ``recall.usable_evidence`` stays at or above its
    threshold and never exceeds ``k_cap`` (what the caller can render whole);
    conflicting or instruction-like memories stay and are flagged.  Exposure is
    what is rendered; ``delivery`` (prompt hook only) predicts whether the
    rendered block survives delivery suppression, so ``delivered`` is logged too.
    ``passthrough`` counts the authorized rows the caller keeps outside the
    decision (never sent); it is logged as ``baseline_features.passthrough``.
    """
    config = _config()
    if not surface_active(RECALL_SURFACE, config):
        return None
    pool = [c for c in candidates if isinstance(c.claim_id, int)][:RECALL_TOP_N]
    cap = len(pool) if k_cap is None else min(int(k_cap), len(pool))
    if not pool or cap < 1:
        _log_skip(RECALL_SURFACE, "no_candidates" if not pool else "no_room", legacy_ids, session_key, engine)
        return None

    from memorymaster.decisions import engine as decisions
    from memorymaster.decisions.questions import build_recall

    refs = [claim_ref(c.claim_id) for c in pool]
    position = {ref: index for index, ref in enumerate(refs, 1)}
    legacy_refs = [claim_ref(cid) for cid in legacy_ids]
    items = [decisions.DecisionItem(ref, "claim", position[ref]) for ref in refs]
    items += [decisions.DecisionItem(ref, "claim", index) for index, ref in enumerate(legacy_refs, 1)
              if ref not in position]
    state, bound = build_recall(query, project_label, [(ref, c.text) for ref, c in zip(refs, pool)])
    seen: dict[str, Any] = {"called": False, "labels": {}}

    def choose(answers: Any) -> Any:
        relevance: dict[str, float] = {}
        for ref in refs:
            value = answers.score("recall.relevant", ref)
            if value is None:
                raise ValueError("relevance answer missing")
            relevance[ref] = float(value)
        order = sorted(refs, key=lambda ref: (-relevance[ref], position[ref]))
        include = _number(answers.threshold("recall.usable_evidence", "include", 0.5), 0.5)
        k_min = int(_number(answers.threshold("recall.usable_evidence", "k_min", 2), 2))
        k_max = int(_number(answers.threshold("recall.usable_evidence", "k_max", 5), 5))
        high = max(0, min(k_max, cap, len(order)))
        low = max(0, min(k_min, high))
        run = 0
        for ref in order:  # include while the evidence is usable
            usable = answers.noul("recall.usable_evidence", ref)
            if usable is None or usable < include:
                break
            run += 1
        conflict_at = _number(answers.threshold("recall.contradicts_request", "label", 0.7), 0.7)
        instruction_at = _number(answers.threshold("recall.instruction_like", "label", 0.7), 0.7)
        labels: dict[str, tuple[str, ...]] = {}
        for ref in refs:
            flags = []
            conflict = answers.noul("recall.contradicts_request", ref)
            instruction = answers.noul("recall.instruction_like", ref)
            if conflict is not None and conflict >= conflict_at:
                flags.append(LABEL_CONFLICT)
            if instruction is not None and instruction >= instruction_at:
                flags.append(LABEL_INSTRUCTION)
            if flags:
                labels[ref] = tuple(flags)
        seen.update(called=True, labels=labels)
        return decisions.JevChoice(order=order, k=min(max(run, low), high), scores=relevance)

    def labels_for(chosen: Sequence[str]) -> dict[int, tuple[str, ...]]:
        return {claim_id_of(ref): seen["labels"][ref] for ref in chosen if ref in seen["labels"]}

    def delivered(exposed: list[str]) -> list[str]:
        if delivery is None:
            return list(exposed)
        ids = [claim_id_of(ref) for ref in exposed]
        if exposed != legacy_refs:  # only the live action exposes something other than legacy
            return list(exposed) if delivery(ids, labels_for(exposed)) else []
        plain = delivery(ids, None)
        flagged = labels_for(exposed) if seen["called"] else {}
        if flagged:
            live = delivery(ids, flagged)
            if live != plain:  # same ids either way: the configured mode decides the rendering
                plain = live if config.mode_for(RECALL_SURFACE) == "live" else plain
        return list(exposed) if plain else []

    context = decisions.DecisionContext(
        kind="hook", session_key=session_key, scope=scope, tenant=tenant,
        legacy_exposed=legacy_refs, delivery_filter=delivered,
        baseline_features={"path": path, "candidates": len(refs), "legacy_exposed": len(legacy_refs),
                           "k_cap": cap, "passthrough": int(passthrough)},
    )
    decision = decisions.decide(RECALL_SURFACE, state=state, questions=bound, items=items,
                                legacy_action=legacy_refs, choose=choose, context=context, engine=engine)
    if decision.mode != "live" or decision.fallback_reason is not None:
        return None
    chosen = [ref for ref in (decision.action or []) if ref in position]
    if not chosen or len(chosen) != len(decision.action):
        return None
    return RecallSelection(decision.decision_id, tuple(claim_id_of(ref) for ref in chosen), labels_for(chosen))


def labelled_row(row: Mapping[str, Any], labels: Sequence[str] | None) -> dict[str, Any]:
    """Copy of a ranked row whose claim text carries ``labels`` (the stored claim is untouched)."""
    copy = dict(row)
    if labels:
        claim = row["claim"]
        copy["claim"] = dataclasses.replace(claim, text=" ".join(labels) + " " + claim.text)
    return copy


def pack_cap(rows: Sequence[Mapping[str, Any]], token_budget: int, output_format: str,
             fixed: Sequence[Mapping[str, Any]] = ()) -> int:
    """Largest ``m`` such that ``fixed`` plus the ``m`` costliest rows, all flagged, pack whole.

    ``fixed`` are the passthrough rows, rendered as they are; if they alone do
    not fit, nothing does (0).
    """
    from memorymaster.recall.context_optimizer import pack_context

    worst = [labelled_row(row, WORST_CASE_LABELS) for row in rows]
    base = list(fixed)
    try:
        costs = [pack_context([row], token_budget=10**9, output_format=output_format).tokens_used for row in worst]
    except ValueError:
        return 0
    ordered = [row for _, row in sorted(zip(costs, worst), key=lambda pair: -pair[0])]
    cap = 0
    for size in range(0 if base else 1, len(ordered) + 1):
        batch = [*base, *ordered[:size]]
        try:
            packed = pack_context(batch, token_budget=token_budget, output_format=output_format)
        except ValueError:
            break
        if packed.claims_included < len(batch):
            break
        cap = size
    return cap


def _claim_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in rows if isinstance(getattr(row.get("claim"), "id", None), int)
            and isinstance(getattr(row.get("claim"), "text", None), str)]


def apply_recall_to_context(result: Any, *, query: str, token_budget: int, output_format: str,
                            project: str, scope: str | None = None, tenant: str | None = None,
                            session_key: str | None = None, allow_sensitive: bool = False,
                            engine: Any = None) -> Any:
    """S2 for MCP ``query_for_context``: ``result`` unchanged unless Jev's live action applies.

    Candidates are the top authorized rows that pass :func:`egress_allowed`; every
    other included row passes through at its legacy position (B1).
    """
    if allow_sensitive or not surface_active(RECALL_SURFACE):
        return result
    try:
        from memorymaster.recall.context_optimizer import ContextResult, pack_context

        public: list[Mapping[str, Any]] = []
        passthrough: list[tuple[int, Mapping[str, Any]]] = []
        for index, row in enumerate(_claim_rows(list(result.rows))):
            if egress_allowed(row["claim"]):
                public.append(row)
            else:  # never offered to Jev; kept at its legacy position
                passthrough.append((index, row))
        pool = public[:RECALL_TOP_N]
        if not pool:
            return result
        cap = pack_cap(pool, token_budget, output_format, fixed=[row for _, row in passthrough])
        selection = decide_recall(
            query, [RecallCandidate(row["claim"].id, row["claim"].text[:MCP_TEXT_CHARS]) for row in pool],
            legacy_ids=[row["claim"].id for row in public], project_label=project, k_cap=cap,
            path="mcp_query_for_context", session_key=session_key, scope=scope, tenant=tenant,
            passthrough=len(passthrough), engine=engine)
        if selection is None:
            return result
        by_id = {row["claim"].id: row for row in pool}
        chosen = merge_passthrough([by_id[cid] for cid in selection.claim_ids], passthrough)
        rendered = merge_passthrough(
            [labelled_row(by_id[cid], selection.labels.get(cid)) for cid in selection.claim_ids], passthrough)
        packed = pack_context(rendered, token_budget=token_budget, output_format=output_format)
        if packed.claims_included != len(rendered):  # never drop an authorized row
            return result
        return ContextResult(output=packed.output, claims_considered=result.claims_considered,
                             claims_included=packed.claims_included, tokens_used=packed.tokens_used,
                             token_budget=packed.token_budget, format=packed.format, rows=tuple(chosen))
    except Exception:
        return result


_TEXT_FOOTER = re.compile(r"\n---\n(\d+)/(\d+) claims \| (\d+)/(\d+) tokens")


def apply_recall_to_receipt(receipt: Any, *, query: str, fetch_claim: Callable[[int], Any], project: str,
                            scope: str | None = None, tenant: str | None = None,
                            session_key: str | None = None, engine: Any = None) -> Any:
    """S2 for MCP ``recall``: re-pack the receipt's claims section with Jev's live action.

    Only the leading claims section changes (derived observation/skill sections
    are kept byte for byte); any doubt about the section boundary keeps legacy.
    Candidates are the top receipt claims that pass :func:`egress_allowed`; every
    other receipt claim passes through at its legacy position (B1).
    """
    if not surface_active(RECALL_SURFACE) or getattr(receipt, "output_format", "") != "text":
        return receipt
    try:
        from memorymaster.recall.context_optimizer import estimate_tokens, pack_context

        entries = list(receipt.claims)
        match = _TEXT_FOOTER.search(receipt.output)
        if not entries or match is None or int(match.group(1)) != len(entries):
            return receipt
        rest = receipt.output[match.end():]
        if rest and not rest.startswith("\n\n"):
            return receipt
        section_budget = int(match.group(4))
        public: list[dict[str, Any]] = []
        passthrough: list[tuple[int, dict[str, Any]]] = []
        for index, entry in enumerate(entries):
            claim = fetch_claim(int(entry["claim_id"]))
            if claim is None or claim.id != entry["claim_id"]:
                return receipt
            if claim.text != entry["text"] or claim.status != entry["status"]:
                return receipt
            if not entry.get("citations"):
                claim = dataclasses.replace(claim, citations=[])
            row = {"claim": claim, "score": _number(entry.get("score"), 0.0), "entry": entry}
            if egress_allowed(claim):
                public.append(row)
            else:  # never offered to Jev; kept at its legacy position
                passthrough.append((index, row))
        pool = public[:RECALL_TOP_N]
        if not pool:
            return receipt
        cap = pack_cap(pool, section_budget, "text", fixed=[row for _, row in passthrough])
        selection = decide_recall(
            query, [RecallCandidate(row["claim"].id, row["claim"].text[:MCP_TEXT_CHARS]) for row in pool],
            legacy_ids=[row["claim"].id for row in public], project_label=project, k_cap=cap,
            path="mcp_recall", session_key=session_key, scope=scope, tenant=tenant,
            passthrough=len(passthrough), engine=engine)
        if selection is None:
            return receipt
        by_id = {row["claim"].id: row for row in pool}
        rendered = merge_passthrough(
            [labelled_row(by_id[cid], selection.labels.get(cid)) for cid in selection.claim_ids], passthrough)
        packed = pack_context(rendered, token_budget=section_budget, output_format="text")
        if packed.claims_included != len(rendered):  # never drop an authorized claim
            return receipt
        output = packed.output + rest
        chosen = merge_passthrough([by_id[cid] for cid in selection.claim_ids], passthrough)
        return dataclasses.replace(receipt, output=output, claims=tuple(row["entry"] for row in chosen),
                                   tokens_used=estimate_tokens(output))
    except Exception:
        return receipt


# ----------------------------------------------------------------- S8 SESSION ---

def decide_session(project: str, pool: Sequence[RecallCandidate], *, legacy_ids: Sequence[int],
                   passthrough_ids: Sequence[int] = (), session_key: str | None = None,
                   scope: str | None = None, engine: Any = None) -> tuple[int, ...] | None:
    """S8: rank recent confirmed claims by relevance to the project; ``None`` keeps legacy.

    ``passthrough_ids`` are legacy claims never offered to Jev (private or
    sensitive, B1): in live mode they keep their legacy slots and Jev's top
    picks fill the others, so the block still holds ``SESSION_TOP_K`` claims.
    Returns the final order, passthrough included.
    """
    if not surface_active(SESSION_SURFACE):
        return None
    kept = {cid for cid in passthrough_ids if isinstance(cid, int)}
    passthrough = [(index, cid) for index, cid in enumerate(legacy_ids) if cid in kept]
    candidates = [c for c in pool if isinstance(c.claim_id, int) and c.claim_id not in kept][:SESSION_POOL_SIZE]
    slots = SESSION_TOP_K - len(passthrough)
    if not candidates or slots < 1:
        _log_skip(SESSION_SURFACE, "no_candidates" if not candidates else "all_passthrough", legacy_ids,
                  session_key, engine)
        return None

    from memorymaster.decisions import engine as decisions
    from memorymaster.decisions.questions import build_session

    refs = [claim_ref(c.claim_id) for c in candidates]
    position = {ref: index for index, ref in enumerate(refs, 1)}
    legacy_refs = [claim_ref(cid) for cid in legacy_ids if cid not in kept]
    items = [decisions.DecisionItem(ref, "claim", position[ref]) for ref in refs]
    items += [decisions.DecisionItem(ref, "claim", index) for index, ref in enumerate(legacy_refs, 1)
              if ref not in position]
    state, bound = build_session(project, [(ref, c.text) for ref, c in zip(refs, candidates)])

    def choose(answers: Any) -> Any:
        scores: dict[str, float] = {}
        for ref in refs:
            value = answers.score("session.relevant_to_project", ref)
            if value is None:
                raise ValueError("relevance answer missing")
            scores[ref] = float(value)
        order = sorted(refs, key=lambda ref: (-scores[ref], position[ref]))
        return decisions.JevChoice(order=order, k=min(slots, len(order)), scores=scores)

    context = decisions.DecisionContext(
        kind="hook", session_key=session_key, scope=scope, legacy_exposed=legacy_refs,
        baseline_features={"path": "session_start", "candidates": len(refs), "legacy_exposed": len(legacy_refs),
                           "passthrough": len(passthrough)},
    )
    decision = decisions.decide(SESSION_SURFACE, state=state, questions=bound, items=items,
                                legacy_action=legacy_refs, choose=choose, context=context, engine=engine)
    if decision.mode != "live" or decision.fallback_reason is not None:
        return None
    chosen = [ref for ref in (decision.action or []) if ref in position]
    if not chosen or len(chosen) != len(decision.action):
        return None
    return tuple(merge_passthrough([claim_id_of(ref) for ref in chosen], passthrough))


# ------------------------------------------------------- Stop-hook joiner ---

def turn_usage_enabled(config: Any = None) -> bool:
    """The Stop hook joins usage whenever RECALL or SESSION is not off."""
    config = config or _config()
    return any(config.mode_for(surface) != "off" for surface in TURN_SURFACES)


def _is_prompt(entry: Mapping[str, Any], message: Mapping[str, Any]) -> bool:
    # isMeta entries (skill bodies, command caveats) are injected by Claude Code, not typed.
    if entry.get("isSidechain") or entry.get("isMeta") or message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        kinds = {block.get("type") for block in content if isinstance(block, Mapping)}
        return "tool_result" not in kinds and "text" in kinds
    return False


def read_last_turn(transcript_path: object, *, tail_bytes: int = TURN_TAIL_BYTES) -> dict[str, Any] | None:
    """The finished turn from a Claude Code transcript: assistant text, tool inputs, timestamp.

    Reads only the last ``tail_bytes`` of the file.  The turn starts after the last
    human prompt; ``observed_at`` is the timestamp of its last assistant entry.
    """
    if not transcript_path:
        return None
    path = Path(str(transcript_path))
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - max(1, int(tail_bytes)))
            handle.seek(start)
            raw = handle.read()
    except OSError:
        return None
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # the first line may be cut in half
    entries: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, Mapping):
            continue
        message = entry.get("message") if isinstance(entry.get("message"), Mapping) else entry
        entries.append((entry, message))
    turn_id = None
    begin = 0
    for index, (entry, message) in enumerate(entries):
        if _is_prompt(entry, message):
            begin, turn_id = index + 1, entry.get("uuid")
    texts: list[str] = []
    inputs: list[str] = []
    budget = {"text": TURN_TEXT_CAP, "inputs": TURN_TEXT_CAP}
    observed = None
    for entry, message in entries[begin:]:
        if entry.get("isSidechain") or message.get("role") != "assistant":
            continue
        content = message.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str) and budget["text"] > 0:
                texts.append(block["text"][: budget["text"]])
                budget["text"] -= len(texts[-1])
            elif block.get("type") == "tool_use" and budget["inputs"] > 0:
                value = block.get("input")
                encoded = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True,
                                                                          default=str)
                inputs.append(encoded[: budget["inputs"]])
                budget["inputs"] -= len(inputs[-1])
        timestamp = entry.get("timestamp")
        if isinstance(timestamp, str) and timestamp.strip():
            observed = timestamp.strip()
    if observed is None or not (texts or inputs):
        return None
    return {"turn_id": turn_id, "assistant_text": "\n".join(texts), "tool_inputs": inputs, "observed_at": observed}


class _ClaimReader:
    """``claim:<id>`` -> ``(human_id, text)`` over ONE read-only connection (mode=ro + query_only)."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.conn: sqlite3.Connection | None = None

    def __enter__(self) -> Callable[[str], "tuple[str | None, str] | None"]:
        if self.path.is_file():
            try:
                from memorymaster.stores._storage_shared import connect_ro

                self.conn = connect_ro(self.path)
            except (sqlite3.Error, OSError):
                self.conn = None
        return self.lookup

    def lookup(self, item_ref: str) -> tuple[str | None, str] | None:
        claim_id = claim_id_of(item_ref)
        if self.conn is None or claim_id is None:
            return None
        try:
            row = self.conn.execute("SELECT human_id, text FROM claims WHERE id = ?", (claim_id,)).fetchone()
        except sqlite3.Error:
            return None
        return (row[0], row[1] or "") if row else None

    def __exit__(self, *_exc: object) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None


def record_stop_turn_usage(data: Mapping[str, Any], *, db_path: str | Path, config: Any = None) -> int:
    """Join the finished turn to this session's RECALL/SESSION exposures (local files only).

    Returns outcome rows written.  Creates nothing when Jev is off or no ledger exists.
    """
    config = config or _config()
    if not turn_usage_enabled(config):
        return 0
    session_key = decision_session_key(data.get("session_id"))
    if session_key is None:
        return 0
    from memorymaster.decisions.ledger import DecisionLedger
    from memorymaster.decisions.outcomes import record_turn_usage

    ledger = DecisionLedger(config.decisions_db)
    if not ledger.exists():
        return 0
    turn = read_last_turn(data.get("transcript_path"))
    if turn is None:
        return 0
    with _ClaimReader(db_path) as lookup:
        return record_turn_usage(ledger, session_key, turn, observed_at=turn["observed_at"], lookup=lookup)


__all__ = [
    "LABEL_CONFLICT",
    "LABEL_INSTRUCTION",
    "RECALL_TOP_N",
    "RecallCandidate",
    "RecallSelection",
    "SESSION_POOL_SIZE",
    "SESSION_TOP_K",
    "WORST_CASE_LABELS",
    "apply_recall_to_context",
    "apply_recall_to_receipt",
    "budget_cap",
    "claim_id_of",
    "claim_ref",
    "decide_recall",
    "decide_session",
    "decision_session_key",
    "labelled_row",
    "merge_passthrough",
    "pack_cap",
    "project_label",
    "read_last_turn",
    "record_stop_turn_usage",
    "surface_active",
    "turn_usage_enabled",
]
