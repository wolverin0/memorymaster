"""Text safety at UTF-8 boundaries, including strings decoded from JSON."""
from __future__ import annotations

import re

_SURROGATES = re.compile(r"[\ud800-\udfff]")


def sanitize_text(text: str) -> str:
    """Replace surrogate code points with U+FFFD; leave all other text unchanged."""
    return _SURROGATES.sub("\ufffd", text)


MAX_BRIEF_BYTES = 1800  # the Codex SessionStart cap, shared by every CLI that delivers the project brief
_TRUNCATED_LINE = "[... truncated]"


def fit_brief(context: str, limit: int = MAX_BRIEF_BYTES) -> str:
    """Cut at the last whole line inside ``limit`` UTF-8 bytes and say so on a final line within the same budget.

    A first non-blank line that alone exceeds the budget keeps the byte-safe cut (never mid-UTF-8 sequence).
    Same policy as the Codex adapter's ``fit_context`` (that template cannot import the package).
    """
    raw = context.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return raw.decode("utf-8", errors="ignore")
    budget = limit - len(("\n" + _TRUNCATED_LINE).encode("utf-8"))
    kept, used = [], 0
    for line in raw.decode("utf-8", errors="ignore").splitlines(keepends=True):
        size = len(line.encode("utf-8"))
        if used + size > budget:
            break
        kept.append(line)
        used += size
    head = "".join(kept).rstrip("\r\n")
    if not head.strip():
        return raw[:limit].decode("utf-8", errors="ignore")
    return head + "\n" + _TRUNCATED_LINE
