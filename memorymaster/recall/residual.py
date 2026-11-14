"""Bounded post-compaction residual: persisted at PreCompact, injected once afterwards.

A context compaction summarizes the conversation and drops what the summary
omits. The PreCompact hook extracts a small residual (open decisions, rejected
options, unverified assumptions, in-flight card ids, the next intended step)
and stores it here. The UserPromptSubmit recall hook then injects it as ONE
bounded block on the first prompt after the compaction and consumes it. The
residual is delivered by session id, never by similarity search.

Delivery does not delete the marker: it is renamed to a sibling "carried" file. The next PreCompact
appends its still-young items, byte for byte, to the new residual (``carry_forward``): the model never
sees them, so nothing it answers can change their text, their age or how many claims they cause. An item
is carried through at most ``MAX_CARRIES`` answered captures. Known cost: an item that was actually
closed can be shown again, up to twice, under a heading that says it may already be closed.

Stdlib only: the recall hook imports this module on every prompt.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from memorymaster.core.hook_log import ENV_STATE_DIR

MAX_ITEMS = 10
MAX_CHARS = 1500
MAX_ITEM_CHARS = 220
TTL_SECONDS = 6 * 3600  # a residual not delivered within hours belongs to a compaction that never happened
CARRY_TTL_SECONDS = 3 * 24 * 3600  # a delivered residual stays available to the next capture for days, not forever
MAX_CARRIES = 2  # answered captures an item is carried through before it is dropped

HEADER = "[MemoryMaster residual] Context was just compacted. These facts from before it may be missing from the summary:"
CARRIED_HEADING = "Possibly still open (from before the previous compaction; may already be closed):"
CARD_RE = re.compile(r"^T-\d{3,5}$")

# kind -> (render heading, order). Order is also the drop priority when the block is over budget.
KINDS: dict[str, str] = {
    "decision": "Open decisions",
    "rejected": "Rejected options",
    "assumption": "Unverified assumptions",
    "card": "In-flight cards",
    "next_step": "Next step",
}


def safe_session(session_id: object) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "", str(session_id or ""))[:64] or "unknown"


def state_dir() -> Path:
    configured = os.environ.get(ENV_STATE_DIR, "").strip()
    return Path(configured) if configured else Path(os.path.expanduser("~")) / ".memorymaster" / "hook_state"


def residual_path(session_id: object, directory: Path | None = None) -> Path:
    return (directory or state_dir()) / f"residual_{safe_session(session_id)}.json"


def carried_path(session_id: object, directory: Path | None = None) -> Path:
    # Own prefix, not residual_*: safe_session allows "_", so "residual_carried_x" could be a real session's marker.
    return (directory or state_dir()) / f"carried_{safe_session(session_id)}.json"


def _clip(text: object, limit: int = MAX_ITEM_CHARS) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _texts(value: object) -> list[str]:
    """Accept a string, a list of strings, or a list of {option/why}-style dicts."""
    if value is None or value == "":
        return []
    if isinstance(value, (str, int, float)):
        return [_clip(value)]
    if isinstance(value, dict):
        value = [value]
    out: list[str] = []
    if isinstance(value, (list, tuple)):
        for entry in value:
            if isinstance(entry, dict):
                option = entry.get("option") or entry.get("text") or entry.get("decision") or entry.get("assumption")
                why = entry.get("why") or entry.get("reason")
                joined = f"{option} (why: {why})" if option and why else (option or why or "")
                if joined:
                    out.append(_clip(joined))
            elif str(entry or "").strip():
                out.append(_clip(entry))
    return out


def bound_residual(raw: Any) -> list[dict[str, str]]:
    """Normalize a model answer into at most MAX_ITEMS ``{"kind", "text"}`` items.

    Unknown keys are ignored, card ids must look like ``T-1234``, duplicates are
    dropped, and each kind keeps its priority order so the hard caps drop the
    least important items first.
    """
    if isinstance(raw, list):
        raw = next((entry for entry in raw if isinstance(entry, dict)), {})
    if not isinstance(raw, dict):
        return []
    items: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, text: str) -> None:
        key = (kind, text.lower())
        if text and key not in seen:
            seen.add(key)
            items.append({"kind": kind, "text": text})

    for text in _texts(raw.get("decisions") or raw.get("open_decisions")):
        add("decision", text)
    for text in _texts(raw.get("rejected") or raw.get("rejected_options")):
        add("rejected", text)
    for text in _texts(raw.get("assumptions") or raw.get("unverified_assumptions")):
        add("assumption", text)
    cards = raw.get("cards") or raw.get("in_flight_cards") or []
    if isinstance(cards, str):
        cards = re.findall(r"T-\d{3,5}", cards)
    for card in cards if isinstance(cards, (list, tuple)) else []:
        card_id = str(card or "").strip().upper()
        if CARD_RE.match(card_id):
            add("card", card_id)
    for text in _texts(raw.get("next_step") or raw.get("next")):
        add("next_step", text)
        break
    return _fit(items)


def render_block(items: list[dict[str, str]]) -> str:
    """The exact text injected: heading per kind, hard-capped at MAX_CHARS."""
    if not items:
        return ""
    lines = [HEADER]
    carried = [item for item in items if item.get("carried")]
    for kind, heading in KINDS.items():
        group = [item["text"] for item in items if item["kind"] == kind and not item.get("carried")]
        if not group:
            continue
        if kind == "card":
            lines.append(f"{heading}: {', '.join(group)}")
        elif kind == "next_step":
            lines.append(f"{heading}: {group[0]}")
        else:
            lines.append(f"{heading}:")
            lines.extend(f"- {text}" for text in group)
    if carried:
        lines.append(CARRIED_HEADING)
        lines.extend(f"- {item['kind']}: {item['text']}" for item in carried)
    return "\n".join(lines)


def _fit(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Drop the lowest-priority items until count and rendered size fit the caps.

    Fresh items take the caps first; carried ones (``carried`` > 0) fill what is left, the oldest dropped first.
    """
    rank = {kind: index for index, kind in enumerate(KINDS)}
    kept = sorted((item for item in items if not item.get("carried")), key=lambda item: rank[item["kind"]])[:MAX_ITEMS]
    while kept and len(render_block(kept)) > MAX_CHARS:
        kept.pop()
    for item in sorted((item for item in items if item.get("carried")), key=lambda item: item["carried"]):
        if len(kept) >= MAX_ITEMS or len(render_block([*kept, item])) > MAX_CHARS:
            break  # strictly oldest-last: nothing older than a dropped item survives
        kept.append(item)
    return kept


def save_residual(
    session_id: object,
    items: list[dict[str, str]],
    *,
    meta: dict[str, object] | None = None,
    directory: Path | None = None,
    now: float | None = None,
) -> Path | None:
    """Atomically write the residual marker for a session; ``None`` when there is nothing to keep."""
    items = _fit(list(items))
    if not items:
        return None
    path = residual_path(session_id, directory)
    payload = {
        "session": safe_session(session_id),
        "created_at": time.time() if now is None else now,
        "items": items,
        "meta": meta or {},
    }
    temporary: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp", prefix="residual_"
        ) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(temporary, path)
    except OSError:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
        return None
    return path


def pending_block(
    session_id: object,
    *,
    directory: Path | None = None,
    now: float | None = None,
) -> str:
    """The block to inject for this session, or ``""``. Reads only; call :func:`consume` after delivery."""
    path = residual_path(session_id, directory)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        age = (time.time() if now is None else now) - float(payload["created_at"])
        if age > TTL_SECONDS:
            consume(session_id, directory=directory, keep=False)  # an expired marker is dead weight: delete it, do not re-parse it
            return ""
        return render_block(_fit([i for i in payload["items"] if i.get("kind") in KINDS and i.get("text")]))
    except (OSError, ValueError, TypeError, KeyError):
        return ""


def consume(session_id: object, *, directory: Path | None = None, keep: bool = True) -> None:
    """Retire the pending marker after delivery. ``keep`` renames it to the carried file; never raises."""
    pending = residual_path(session_id, directory)
    try:
        if keep:
            os.replace(pending, carried_path(session_id, directory))  # atomic; keeps the saved timestamp
        else:
            pending.unlink(missing_ok=True)
    except FileNotFoundError:
        pass
    except OSError:
        # The rename failed (on Windows, the carried file held open). Never leave the STALE carried copy in
        # place of the delivered one: end with no carry rather than the wrong one, and never deliver twice.
        if keep:
            _discard(carried_path(session_id, directory))
        _discard(pending)


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        try:
            path.write_text("{}", encoding="utf-8")  # unreadable as a residual: previous_items ignores it
        except OSError:
            pass


def previous_items(
    session_id: object,
    *,
    directory: Path | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Items of this session's latest residual, for the next capture; ``[]`` on anything unreadable.

    An undelivered marker is newer than a carried one (it was built after it), so it wins. Every item is
    revalidated (kind, text, card id) and clipped, and at most MAX_ITEMS are returned, whatever the file holds.
    """
    current = time.time() if now is None else now
    for path, ttl in ((residual_path(session_id, directory), TTL_SECONDS), (carried_path(session_id, directory), CARRY_TTL_SECONDS)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if current - float(payload["created_at"]) > ttl:
                continue
            out: list[dict[str, Any]] = []
            for item in payload["items"]:
                if len(out) >= MAX_ITEMS:
                    break
                if not isinstance(item, dict) or item.get("kind") not in KINDS or not isinstance(item.get("text"), str):
                    continue
                text = _clip(item["text"])
                age = item.get("carried") or 0
                if not isinstance(age, int) or not 0 <= age <= MAX_CARRIES:
                    continue  # a tampered age never extends a life: the item is dropped
                if text and (item["kind"] != "card" or CARD_RE.match(text)):
                    out.append({"kind": item["kind"], "text": text, "carried": age})
            if out:
                return out
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    return []


def _same(item: dict[str, Any]) -> tuple[str, str]:
    """Identity for the exact-duplicate rule: kind plus the text ignoring case and whitespace (cards: the id)."""
    return item["kind"], " ".join(item["text"].lower().split())


def carry_forward(fresh: list[dict[str, Any]], previous: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The new residual: the fresh items, then the previous items that are still young, copied as they are.

    Called once per ANSWERED capture, so the age goes up by one there and nowhere else. A previous item is
    dropped once it would be carried through more than ``MAX_CARRIES`` captures, or when a fresh item (or an
    earlier carried one) is exactly the same kind and text; nothing fuzzy, so a wrong match is impossible.
    """
    taken = {_same(item) for item in fresh}
    carried: list[dict[str, Any]] = []
    for item in previous:
        age = int(item.get("carried") or 0) + 1
        if age <= MAX_CARRIES and _same(item) not in taken:
            taken.add(_same(item))
            carried.append({"kind": item["kind"], "text": item["text"], "carried": age})
    return _fit([*fresh, *carried])


def retire_carried(session_id: object, *, directory: Path | None = None) -> None:
    """Drop the carried file once its items live on in a saved marker or have aged out."""
    try:
        carried_path(session_id, directory).unlink(missing_ok=True)
    except OSError:
        pass


def sweep_carried(*, directory: Path | None = None, now: float | None = None) -> None:
    """Delete carried files past their TTL. Called at capture time only (never on the prompt path), so a
    session that never compacts again keeps its carried file until some later capture sweeps the directory."""
    cutoff = (time.time() if now is None else now) - CARRY_TTL_SECONDS
    try:
        for path in (directory or state_dir()).glob("carried_*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:
        pass
