"""Egress redaction for claim text sent to the profile map provider (F-03).

When the compiled profile reads governed claims, their text leaves for an
external model. It is redacted first: the canonical credential redactor
(``core.security.redact_text``), RFC1918 private IPv4 including bare IPs in
prose, home/absolute paths and email addresses. A claim is then blocked (never
sent) when a credential-grade finding survives redaction:

* a secret-pattern finding in any encoded variant, e.g. a base64 credential
  that cannot be substituted in place;
* anything dream_bridge refuses to export (``_DREAM_EXTRA_PATTERNS``, the same
  compiled pattern): ``DB_PASS=``-style assignments, Supabase/SendGrid/Twilio
  keys, webhook URLs with tokens, SSH command shapes, public ``IP:port``, and
  text already carrying a ``[REDACTED`` marker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from memorymaster.bridges.dream_bridge import _DREAM_EXTRA_PATTERNS
from memorymaster.core.security import (
    _CLAIM_ONLY_PATTERNS,
    redact_text,
    scan_text_for_findings,
)

# A closing parenthesis ends a user name: "(/Users/alice)" keeps its ")".
_PATH_SEGMENT = r"[^\s/\\\"'`<>|,;)]+"
_EGRESS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Private IPv4 (bare, in prose too) and Windows absolute/UNC paths.
    *_CLAIM_ONLY_PATTERNS,
    # POSIX-style home paths the patterns above miss: /home/<user>,
    # /Users/<user> or /users/<user>, Git Bash /c/Users/<user>, WSL
    # /mnt/<drive>/Users/<user> in any case and with a spaced user name, and
    # ~<user> with or without a path (not ~/ or ~5). Not inside URLs, and not
    # REST placeholders such as /users/:id (ruling R3).
    ("home_path", re.compile(
        r"(?i)(?<![\w.~-])/(?:mnt/)?(?:[a-z]/)?(?:home|users)/(?![:{<\[$*])"
        r"(?:" + _PATH_SEGMENT + r"(?: " + _PATH_SEGMENT + r"){1,2}(?=/)|" + _PATH_SEGMENT + r")"
        r"|(?<![\w/~-])~[a-z_][\w-]*(?:\.[\w-]+)*"
    )),
    ("email", re.compile(
        r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
    )),
)
# Markers this module writes. They are removed before the export check, whose
# ``[REDACTED`` alternative would otherwise refuse every redacted claim.
_EGRESS_MARKER = re.compile(r"\[REDACTED:[A-Za-z0-9_]+\]")


@dataclass(frozen=True, slots=True)
class ClaimEgress:
    text: str
    findings: tuple[str, ...]
    blocked: bool


def prepare_claim_egress(text: str) -> ClaimEgress:
    """Redact ``text`` for the map provider; ``blocked`` means do not send it."""
    redacted, literal = redact_text(text)
    findings = list(literal)
    for name, pattern in _EGRESS_PATTERNS:
        redacted, count = pattern.subn(f"[REDACTED:{name}]", redacted)
        if count:
            findings.append(name)
    blocked = bool(scan_text_for_findings(redacted))
    if "[redacted" in text.lower() or _DREAM_EXTRA_PATTERNS.search(_EGRESS_MARKER.sub(" ", redacted)):
        findings.append("export_refused")
        blocked = True
    return ClaimEgress(redacted, tuple(dict.fromkeys(findings)), blocked)


__all__ = ["ClaimEgress", "prepare_claim_egress"]
