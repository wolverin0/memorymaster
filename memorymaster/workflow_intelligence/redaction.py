"""Bounded, reusable redaction for transcript-derived workflow evidence."""

from __future__ import annotations

import re

from memorymaster.core.security import redact_text


_WINDOWS_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/])[^\r\n\t\"'<>|]+"
)
_POSIX_HOME = re.compile(r"(?<![A-Za-z0-9])/(?:home|Users)/[^/\s]+/[^\s\"'<>|]+")
_PRIVATE_IPV4 = re.compile(
    r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
)
_WHITESPACE = re.compile(r"\s+")


def public_excerpt(value: object, *, limit: int = 400) -> str:
    """Return a single-line, secret/path/private-IP redacted excerpt."""
    text = str(value or "")
    text, _ = redact_text(text)
    text = _WINDOWS_PATH.sub("[REDACTED:absolute_path]", text)
    text = _POSIX_HOME.sub("[REDACTED:absolute_path]", text)
    text = _PRIVATE_IPV4.sub("[REDACTED:private_ip]", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return text[: max(0, min(limit, 400))]
