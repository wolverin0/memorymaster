"""Deterministic extraction units and bounded windows over a capture's messages (T-1864).

A capture's messages are cut into *units* (a message, or a fixed-size piece of a very
long one) whose indexes never change when messages are appended. The extraction cursor
is a unit count: everything before it was sent to the extractor, everything after it was
not. A window is a contiguous run of units; consecutive pieces of one message are joined
back into an exact slice of the original text, so evidence quotes stay verbatim.
"""

from __future__ import annotations

from typing import Any

# Matches the old per-message truncation size; longer messages become several units.
PIECE_CHARS = 16_000


def _piece_end(text: str, start: int) -> int:
    end = min(len(text), start + PIECE_CHARS)
    if end < len(text):
        cut = max(text.rfind("\n", start, end), text.rfind(" ", start, end))
        if cut >= start + PIECE_CHARS * 3 // 4:
            end = cut + 1
    return end


def units(messages: list[dict[str, Any]]) -> list[tuple[int, int, int]]:
    """(message index, start, end) for every unit, in order."""
    result: list[tuple[int, int, int]] = []
    for index, message in enumerate(messages):
        text = str(message.get("text", ""))
        start = 0
        while True:
            end = _piece_end(text, start)
            result.append((index, start, end))
            if end >= len(text):
                break
            start = end
    return result


def next_window(
    messages: list[dict[str, Any]], cursor: int, max_chars: int,
) -> tuple[list[dict[str, Any]], int]:
    """Messages for the units from ``cursor`` (at least one, at most ``max_chars``) and the new cursor."""
    all_units = units(messages)
    taken: list[tuple[int, int, int]] = []
    used = 0
    for unit in all_units[cursor:]:
        size = unit[2] - unit[1]
        if taken and used + size > max_chars:
            break
        taken.append(unit)
        used += size
    window: list[dict[str, Any]] = []
    for index, start, end in taken:
        text = str(messages[index].get("text", ""))[start:end]
        if window and window[-1]["_index"] == index:
            window[-1]["text"] += text
        else:
            window.append({**messages[index], "text": text, "_index": index})
    return [{key: value for key, value in message.items() if key != "_index"} for message in window], cursor + len(taken)


def extraction_complete(row: dict[str, Any]) -> bool:
    """Whether every unit of the capture was sent to the extractor.

    A NULL cursor is a historical row (or a backfill window) whose stored extraction is
    complete by definition: it is never re-windowed.
    """
    cursor = row.get("extracted_through")
    return cursor is None or int(cursor) >= len(units(list(row.get("messages") or [])))
