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
import secrets
import string
from collections.abc import Callable
from dataclasses import dataclass

from memorymaster.bridges.dream_bridge import _DREAM_EXTRA_PATTERNS
from memorymaster.core.security import (
    _CLAIM_ONLY_PATTERNS,
    _SECRET_PATTERNS,
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


_TOKEN_CHAR = re.compile(r"[A-Za-z0-9_-]")
# Marker names this machinery can actually emit: trusted neutralisation accepts no other name.
_EMITTED_NAMES = frozenset(
    {name for name, _ in _SECRET_PATTERNS}
    | {name for name, _ in _CLAIM_ONLY_PATTERNS}
    | {name for name, _ in _EGRESS_PATTERNS}
    | {"encoded_secret", "structured_secret", "absolute_path", "home_path", "numeric", "private_ip"}
)
_INDEXED_MARKER = re.compile(r"\[REDACTED:[A-Za-z0-9_]+(?:#\d+)?\]")


def _neutralise(text: str, *, trusted: bool) -> tuple[str, Callable[[str], str], bool]:
    """Swap well-formed ``[REDACTED:<name>]`` markers for a per-call placeholder; return the restorer.

    Untrusted (default): a marker with a token character (``[A-Za-z0-9_-]``) on either side is spliced
    into a token, so it stays in place and the claim gate blocks it. ``trusted`` (text built from
    output of this very gate) neutralises every well-formed marker, adjacent letters or not.
    """
    tag = "".join(secrets.choice(string.ascii_letters) for _ in range(12))
    while tag in text:
        tag = "".join(secrets.choice(string.ascii_letters) for _ in range(12))
    held: list[str] = []
    spliced: list[bool] = []

    def hold(match: re.Match[str]) -> str:
        start, end = match.span()
        glued = bool(
            (start and _TOKEN_CHAR.match(text[start - 1])) or (end < len(text) and _TOKEN_CHAR.match(text[end]))
        )
        if trusted and match.group(0)[len("[REDACTED:") : -1] not in _EMITTED_NAMES:
            return match.group(0)  # a name our gate never emits is text: it blocks
        if glued and not trusted:
            return match.group(0)
        if glued:
            # Trusted but spliced into a token: the token without the marker is what a secret looks like.
            left = re.search(r"\S*$", text[:start]).group(0)
            right = re.match(r"\S*", text[end:]).group(0)
            joined = left + right
            if scan_text_for_findings(joined) or redact_text(joined)[1]:
                spliced.append(True)
        held.append(match.group(0))
        return f"[{tag}{len(held) - 1}]"

    neutral = _EGRESS_MARKER.sub(hold, text)
    pattern = re.compile(r"\[" + tag + r"(\d+)\]")
    restore = (lambda out: pattern.sub(lambda m: held[int(m.group(1))], out)) if held else (lambda out: out)
    return neutral, restore, bool(spliced)


def prepare_transcript_egress(text: str, *, trusted: bool = False) -> ClaimEgress:
    """``prepare_claim_egress`` for STORED text that carries OUR redaction markers.

    The claim gate refuses any text containing ``[redacted``; well-formed markers are neutralised
    first and restored after (see ``_neutralise`` for what well-formed means and for ``trusted``).
    A spliced, malformed or foreign marker still blocks. Raw transcripts must use
    ``prepare_claim_egress`` directly.
    """
    neutral, restore, spliced = _neutralise(text, trusted=trusted)
    egress = prepare_claim_egress(neutral)
    findings = (*egress.findings, "spliced_marker") if spliced else egress.findings
    return ClaimEgress(restore(egress.text), findings, egress.blocked or spliced)


def prepare_indexed_egress(texts: list[str]) -> list[ClaimEgress]:
    """Gate several STORED texts that a model will compare, with stable pseudonyms.

    Redacted private IPs, paths and emails become ``[REDACTED:<name>#<k>]`` where the same original
    value gets the same ``k`` across ALL ``texts`` and a different value a different ``k``, so the
    model can tell two redacted values apart without seeing them. Blocking is the claim gate's: only a
    credential-grade finding that survives redaction (literal credentials are substituted, like there).
    """
    seen: dict[tuple[str, str], int] = {}
    out: list[ClaimEgress] = []
    for text in texts:
        neutral, restore, _spliced = _neutralise(text, trusted=False)
        redacted, findings = neutral, []
        # Indexed substitution first: the literal redactor also masks home paths without an index.
        for name, pattern in _EGRESS_PATTERNS:

            def index(match: re.Match[str], name: str = name) -> str:
                return f"[REDACTED:{name}#{seen.setdefault((name, match.group(0)), len(seen) + 1)}]"

            redacted, count = pattern.subn(index, redacted)
            if count:
                findings.append(name)
        redacted, literal = redact_text(redacted)
        findings.extend(literal)
        blocked = bool(scan_text_for_findings(redacted))
        if "[redacted" in neutral.lower() or _DREAM_EXTRA_PATTERNS.search(_INDEXED_MARKER.sub(" ", redacted)):
            findings.append("export_refused")
            blocked = True
        out.append(ClaimEgress(restore(redacted), tuple(dict.fromkeys(findings)), blocked))
    return out


__all__ = ["ClaimEgress", "prepare_claim_egress", "prepare_indexed_egress", "prepare_transcript_egress"]
