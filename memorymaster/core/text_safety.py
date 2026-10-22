"""Text safety at UTF-8 boundaries, including strings decoded from JSON."""
from __future__ import annotations

import re

_SURROGATES = re.compile(r"[\ud800-\udfff]")


def sanitize_text(text: str) -> str:
    """Replace surrogate code points with U+FFFD; leave all other text unchanged."""
    return _SURROGATES.sub("\ufffd", text)
