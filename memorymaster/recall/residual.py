"""Bounded post-compaction residual: persisted at PreCompact, injected once afterwards.

A context compaction summarizes the conversation and drops what the summary
omits. The PreCompact hook extracts a small residual (open decisions, rejected
options, unverified assumptions, in-flight card ids, the next intended step)
and stores it here. The UserPromptSubmit recall hook then injects it as ONE
bounded block on the first prompt after the compaction and consumes it. The
residual is delivered by session id, never by similarity search.

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

ENV_STATE_DIR = "MEMORYMASTER_HOOK_STATE_DIR"
MAX_ITEMS = 10
MAX_CHARS = 1500
MAX_ITEM_CHARS = 220
TTL_SECONDS = 6 * 3600  # a residual not delivered within hours belongs to a compaction that never happened

HEADER = "[MemoryMaster residual] Context was just compacted. These facts from before it may be missing from the summary:"
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
    for kind, heading in KINDS.items():
        group = [item["text"] for item in items if item["kind"] == kind]
        if not group:
            continue
        if kind == "card":
            lines.append(f"{heading}: {', '.join(group)}")
        elif kind == "next_step":
            lines.append(f"{heading}: {group[0]}")
        else:
            lines.append(f"{heading}:")
            lines.extend(f"- {text}" for text in group)
    return "\n".join(lines)


def _fit(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Drop the lowest-priority items until count and rendered size fit the caps."""
    rank = {kind: index for index, kind in enumerate(KINDS)}
    kept = sorted(items, key=lambda item: rank[item["kind"]])[:MAX_ITEMS]
    while kept and len(render_block(kept)) > MAX_CHARS:
        kept.pop()
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
            consume(session_id, directory=directory)  # an expired marker is dead weight: delete it, do not re-parse it
            return ""
        return render_block(_fit([i for i in payload["items"] if i.get("kind") in KINDS and i.get("text")]))
    except (OSError, ValueError, TypeError, KeyError):
        return ""


def consume(session_id: object, *, directory: Path | None = None) -> None:
    try:
        residual_path(session_id, directory).unlink(missing_ok=True)
    except OSError:
        pass
