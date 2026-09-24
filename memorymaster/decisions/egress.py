"""The single egress redactor for text that leaves the machine toward TypeSafe.

``prepare_egress`` composes the ingest secret patterns (``core.security``) with
privacy rules the ingest filter deliberately does not apply (private IPs are kept
in claims on purpose): private IPv4/CGNAT, IPv6 ULA/link-local, email addresses
(also percent-encoded), phone numbers (E.164 and Argentine national formats, also
after a label or in a ``tel:`` URI), home paths in every spelling (shells, URLs,
UNC and admin shares, NAS layouts, relative at any depth and percent-encoded
forms; the whole user name, including apostrophes, dots and hyphens), internal hostnames,
URL userinfo and URL query secrets, short numeric credentials after a keyword,
and opaque tokens (>= 20 chars) near a secret keyword or with Shannon entropy >= 4.0.

Redaction runs before truncation.  The result is then re-scanned with every
encoded variant (base64, hex escapes, confusables, embedded JSON, URL-decoding);
if a credential-grade finding survives, the text is withheld (``blocked``) and the
caller must not send the request.  ISO dates/times, public URLs, version strings
and ordinary identifiers are left untouched (review F-10 false positives).
"""
from __future__ import annotations

import math
import re
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from memorymaster.core.security import _SECRET_PATTERNS, redact_text, scan_text_for_findings

MAX_FIELD_CHARS = 1_200
TRUNCATED_MARKER = "[REDACTED:truncated_value]"
_PRIVACY_FINDINGS = frozenset({"home_path_windows", "home_path_unix", "private_ip_port"})
CREDENTIAL_FINDINGS: frozenset[str] = frozenset(name for name, _ in _SECRET_PATTERNS) - _PRIVACY_FINDINGS

_Replacement = Callable[[re.Match[str]], str] | str

# A closing parenthesis, bracket, brace or backtick ends a user name: "(/mnt/c/Users/alice)"
# and "`/home/alice`" keep their closing delimiter.  An apostrophe between letters is
# part of it ("dan_o'neil"), a quote around a path is not.
_APOSTROPHE = r"(?<=\w)'(?=\w)"
_PATH_CHARS = rf"(?:[^\\/\s\"'<>|:*?)\]}}`]|{_APOSTROPHE})"
_WIN_USER = rf"(?:{_PATH_CHARS}+(?: {_PATH_CHARS}+){{1,2}}(?=\\|/)|{_PATH_CHARS}+)"
_POSIX_CHARS = rf"(?:[^/\s\"'<>|)\]}}`]|{_APOSTROPHE})"
_POSIX_USER = rf"(?:{_POSIX_CHARS}+(?: {_POSIX_CHARS}+){{1,2}}(?=/)|{_POSIX_CHARS}+)"
# Where an absolute home path may start: not inside a word, host or path, but after
# a relative "." or ".." segment at any depth ("./home/<u>", "../../home/<u>",
# "x/../home/<u>" all name someone's home).
_PATH_START = r"(?:(?<![\w.~])|(?<=(?<![\w.~])\.)|(?<=(?<![\w.~])\.\.))"
# The home segment of a URL or slash-UNC path, also a hidden share (home$, users$); a
# lowercase "users" without "$" is a REST path there, not a home.
_URL_HOME = r"(?:(?i:homes?\$?|users\$)|Users)"
# Windows separators, plain or JSON-escaped, and the forward slash.
_WIN_SEP = r"(?:\\{1,2}|/)"

_PHONE_DIGITS = (8, 15)
# A phone number may follow a label ("tel:", "WhatsApp:", "cel.") or sit in a path
# ("phone/+54..."), but not a digit's "." ":" or "/" (versions, times, ports, dates).
_PHONE_START = r"(?<![\w+-])(?<!\d[.:/])"


def _phone(match: re.Match[str]) -> str:
    digits = sum(c.isdigit() for c in match.group(0))
    low, high = _PHONE_DIGITS
    return "[REDACTED:phone]" if low <= digits <= high else match.group(0)


def _ar_phone(match: re.Match[str]) -> str:
    """An Argentine national number has 10 digits without the trunk 0 and the mobile 15,
    and the only two-digit area code is 11; any other run is kept (counts, ids, ranges)."""
    area = match.group("pa") or match.group("ta") or match.group("na") or ""
    local = match.group("pl") or match.group("tl") or match.group("nl") or ""
    area = area[1:] if area.startswith("0") else area
    local_digits = "".join(c for c in local if c.isdigit())
    if area == "15" and match.group("na") and "-" in match.group(0) and 7 <= len(local_digits) <= 8:
        return "[REDACTED:phone]"  # a local mobile with the 15 prefix and no area: 15-1234-5678
    national = area + local_digits
    if len(national) != 10 or (len(area) == 2 and area != "11"):
        return match.group(0)
    return "[REDACTED:phone]"


# These run BEFORE the core secret patterns so a home path such as
# ``C:\Users\John Smith\...`` is replaced whole (the core pattern stops at the
# space).  Order matters: home paths under a URL or //host first (whole, host
# included), URL userinfo before emails, specific home-path spellings before the
# generic POSIX one, UNC homes and the WSL home spelling before UNC hosts.
_RULES: tuple[tuple[str, re.Pattern[str], _Replacement], ...] = (
    (
        # A home directory under a URL or slash-UNC authority, whole (scheme and host
        # too): smb://host/home/<u>, file://host/homes/<u>, https://host:port/Users/<u>,
        # https://nas:5001/volume1/homes/<u>, //host/home/<u>, //host/c$/Users/<u>,
        # file:////host/home/<u> (UNC in a file URL).  Lowercase /users/ in a URL is a
        # REST path, not a home.
        "home_path",
        re.compile(
            r"(?:(?<![\w.+-])(?i:[a-z][a-z0-9+.\-]*):/{2,5}|(?<![\w:/.])/{2,4})[^/\s\"'<>\\]+"
            rf"(?:/(?i:volume\d+|share|webdav|[a-z]\$))?/{_URL_HOME}/{_POSIX_USER}"
        ),
        "[REDACTED:home_path]",
    ),
    (
        # A file share names any share before the home: smb://nas/data/home/<u>,
        # //host/share/home/<u> (the slash spelling of \\host\share\home\<u>).
        "home_path",
        re.compile(
            r"(?:(?<![\w.+-])(?i:smb|cifs|nfs|afp|file):/{2,5}|(?<![\w:/.])/{2,4})"
            rf"[^/\s\"'<>\\]+/[^/\s\"'<>\\]+/{_URL_HOME}/{_POSIX_USER}"
        ),
        "[REDACTED:home_path]",
    ),
    (
        "url_userinfo",
        re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s@:\[\]]+(?::[^/\s@]*)?@"),
        r"\1[REDACTED:url_userinfo]@",
    ),
    (
        "url_secret",
        re.compile(
            r"(?i)([?&](?:api[_-]?key|apikey|key|token|access[_-]?token|auth|sig|signature|secret"
            r"|client[_-]?secret|password|passwd|pwd|code)=)[^&\s#\[]+"
        ),
        r"\1[REDACTED:url_secret]",
    ),
    (
        "email",
        re.compile(r"(?<![\w.+%-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"),
        "[REDACTED:email]",
    ),
    (
        # C:\Users\<u>, C:\Documents and Settings\<u>, and the MSYS2/Cygwin homes
        # C:\msys64\home\<u>, C:/cygwin64/home/<u>.
        "home_path",
        re.compile(rf"(?i)(?<![\w])[a-z]:{_WIN_SEP}(?:Users|Documents and Settings"
                   rf"|(?:msys(?:32|64)?|cygwin(?:64)?){_WIN_SEP}home){_WIN_SEP}{_WIN_USER}"),
        "[REDACTED:home_path]",
    ),
    (
        # Windows relative spellings at any depth: ..\..\Users\<u>, .\Users\<u>.
        "home_path",
        re.compile(rf"(?i)(?<![\w.~])(?:\.{{1,2}}(?:\\{{1,2}}|/))+(?:Users|Documents and Settings)\\{{1,2}}{_WIN_USER}"),
        "[REDACTED:home_path]",
    ),
    (
        # Rooted without a drive, plain or JSON-escaped: \Users\<u>, \\Users\\<u> (before
        # the UNC host rule, which would otherwise take "Users" for a host and keep <u>).
        # REST placeholders such as \users\{id} are not homes.
        "home_path",
        re.compile(rf"(?<![\w\\.$:])\\{{1,2}}(?:Users|Documents and Settings)\\{{1,2}}(?![:{{<\[$*]){_WIN_USER}"),
        "[REDACTED:home_path]",
    ),
    (
        # UNC home shares, plain or JSON-escaped, also under a share or an admin share, and
        # hidden shares: \\host\home\<u>, \\host\Users\<u>, \\host\c$\Users\<u>,
        # \\host\share\home\<u>, \\host\home$\<u>, \\host\users$\<u>.
        "home_path",
        re.compile(rf"(?i)(?<!\\)\\{{2,4}}(?!wsl\$|wsl\.localhost)[^\\/\s]+(?:\\{{1,2}}[^\\/\s]+)?"
                   rf"\\{{1,2}}(?:homes?|users)\$?\\{{1,2}}{_WIN_USER}"),
        "[REDACTED:home_path]",
    ),
    (
        # WSL from Windows, backslash, JSON-escaped or slash spelling: the Linux home
        # \\wsl$\<distro>\home\<u> and the Windows home seen from WSL
        # \\wsl$\<distro>\mnt\<drive>\Users\<u> (also \\wsl.localhost\...).
        "home_path",
        re.compile(rf"(?i)(?:\\{{2,4}}|//)(?:wsl\$|wsl\.localhost){_WIN_SEP}[^\\/\s]+{_WIN_SEP}"
                   rf"(?:home|mnt{_WIN_SEP}[a-z]{_WIN_SEP}Users){_WIN_SEP}{_WIN_USER}"),
        "[REDACTED:home_path]",
    ),
    (
        # Git Bash /c/Users/<u> and WSL /mnt/<drive>/Users/<u>, any case, with the
        # same spaced-user-name rule as the Windows spelling (ruling R3).
        "home_path",
        re.compile(rf"(?i){_PATH_START}/(?:mnt/)?[a-z]/Users/{_WIN_USER}"),
        "[REDACTED:home_path]",
    ),
    (
        # NAS home layouts: Synology /volume<N>/homes/<u>, QNAP /share/homes/<u>, /homes/<u>.
        "home_path",
        re.compile(rf"(?i){_PATH_START}(?:/volume\d+|/share)?/homes/{_POSIX_USER}"),
        "[REDACTED:home_path]",
    ),
    (
        # macOS homes on another volume: /Volumes/<disk>/Users/<u>, a spaced disk name too.
        "home_path",
        re.compile(rf"(?i){_PATH_START}/Volumes/[^/\s\"'<>|]+(?: [^/\s\"'<>|]+){{0,3}}/Users/{_POSIX_USER}"),
        "[REDACTED:home_path]",
    ),
    (
        # /home/<u>, /Users/<u> and /users/<u> in any case (R3), also /var/home/<u>
        # (Fedora Atomic) and /export/home/<u>, a spaced user name whole; REST
        # placeholders such as /users/:id or /users/{id} are not home directories.
        "home_path",
        re.compile(rf"(?i){_PATH_START}(?:(?:/var|/export)?/home|/users)/(?![:{{<\[$*]){_POSIX_USER}"),
        "[REDACTED:home_path]",
    ),
    (
        "home_path",
        # ~<u>, the whole name including an apostrophe between letters (~dan_o'neil).
        re.compile(rf"(?<![\w/~])~[A-Za-z_](?:[A-Za-z0-9_.-]|{_APOSTROPHE}){{0,31}}(?!\w|'\w)"),
        "[REDACTED:home_path]",
    ),
    (
        "unc_host",
        re.compile(r"(?<![^\s\"'(=])\\\\(?!wsl\$|wsl\.localhost|\?\\|\.\\)([^\\\s]+)(?=\\)"),
        r"\\\\[REDACTED:unc_host]",
    ),
    (
        "private_ip",
        re.compile(
            r"(?<![\d.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
            r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
            r"|192\.168\.\d{1,3}\.\d{1,3}"
            r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}"
            r"|169\.254\.\d{1,3}\.\d{1,3})(?!\d|\.\d)"
        ),
        "[REDACTED:private_ip]",
    ),
    (
        "private_ipv6",
        re.compile(
            r"(?i)(?<![\w:])(?:f[cd][0-9a-f]{2}|fe[89ab][0-9a-f]):(?:[0-9a-f]{0,4}:){1,6}[0-9a-f]{0,4}"
            r"(?:%[\w.]+)?(?![\w:])"
        ),
        "[REDACTED:private_ipv6]",
    ),
    (
        # E.164 (+<country> and 8-15 digits, spaces/dots/hyphens/parentheses allowed),
        # also "tel:+54...", "WhatsApp:+54..."; a country code never starts with 0
        # ("+0.5" is a signed number).  Checked by digit count in _phone above.
        "phone",
        re.compile(_PHONE_START + r"\+[1-9]\d{0,2}(?:[ .-]?\(?\d{1,4}\)?){2,5}(?!\w|[.-]\d)"),
        _phone,
    ),
    (
        # Argentine national formats, always with an area code or mobile prefix, hyphens
        # or spaces: 011 4567-8901, (0351) 456-7890, 0351 4567890, 11-1234-5678,
        # 11 1234 5678, 11-15-1234-5678; "tel:011-...", "cel.11-...".  _ar_phone keeps a
        # run that is not 10 national digits (ranges, ids, counts, ISO dates).
        "phone",
        re.compile(
            _PHONE_START
            + r"(?:\((?P<pa>0?\d{2,4})\)[ -]?(?P<pl>\d{2,4}[ -]?\d{4})"
            r"|(?P<ta>0\d{2,4})[ -](?P<tl>\d{2,4}[ -]?\d{4})"
            r"|(?P<na>\d{2,4})[ -](?:15[ -])?(?P<nl>\d{3,4}[ -]\d{4}))"
            r"(?![\w-]|\.\d)"
        ),
        _ar_phone,
    ),
    (
        # WhatsApp links carry the E.164 digits without "+": wa.me/<n>, ?phone=<n>.
        "phone",
        re.compile(r"(?i)(?:(?<=\bwa\.me/)|(?<=[?&]phone=))(?:\+|%2B)?\d{8,15}(?!\w)"),
        "[REDACTED:phone]",
    ),
    (
        "internal_host",
        re.compile(
            r"(?i)(?<![\w.@-])(?:[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.)+"
            r"(?:local|lan|internal|corp|home\.arpa|home|intranet|localdomain)"
            r"(?![\w-]|\.[a-z0-9]|\()"
        ),
        "[REDACTED:internal_host]",
    ),
)

# A run holding a percent escape ("C%3A%5CUsers%5C<u>", "alice%40example.com",
# "tel:%2B54..."): decoded before the home-path, email and phone rules look at it, and
# replaced whole when the decoded form holds a home path, an email address or a phone.
_TOKEN_RUN = re.compile(rf"(?<![^\s\"'<>])(?:[^\s\"'<>]|{_APOSTROPHE})+")
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_MAX_PERCENT_DECODES = 3
_DECODED_LABELS = ("home_path", "email", "phone")


def _redact_encoded_home_paths(text: str, counts: Counter[str]) -> str:
    decoded_rules = [(label, pattern, replacement) for wanted in _DECODED_LABELS
                     for label, pattern, replacement in _RULES if label == wanted]

    def replace(match: re.Match[str]) -> str:
        token = decoded = match.group(0)
        if not _PERCENT_ESCAPE.search(token):
            return token
        for _ in range(_MAX_PERCENT_DECODES):  # double encoding too
            again = urllib.parse.unquote(decoded)
            if again == decoded:
                break
            decoded = again
        if decoded != token:
            for label, rule, replacement in decoded_rules:
                # A phone candidate with the wrong digit count is kept by its replacement.
                if rule.sub(replacement, decoded) != decoded:
                    counts[label] += 1
                    return f"[REDACTED:{label}]"
        return token

    return _TOKEN_RUN.sub(replace, text) if "%" in text else text


_SHORT_CREDENTIAL = re.compile(
    r"(?i)\b(?:pin|passcode|otp|password|passwd|pwd|passphrase|contrase\u00f1a)\b"
    r"(?:\s+(?:is|was|es|code|number))?\s*[:=]?\s*(?P<value>[^\s,;]{4,64})"
)
_SECRET_KEYWORD = re.compile(
    r"(?i)(?<![a-z])(?:api[_ -]?keys?|apikeys?|keys?|tokens?|secrets?|passwords?|passwd|credentials?"
    r"|bearer|auth|private)(?![a-z])"
)
_MARKER = re.compile(r"\[REDACTED:[a-z0-9_]+\]")
_URL_SPAN = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s<>\"'`]+")
_TOKEN_OUTSIDE_URL = re.compile(r"[A-Za-z0-9+/=%]{20,}")
_TOKEN_INSIDE_URL = re.compile(r"[A-Za-z0-9%]{20,}")
_CAMEL_CHUNK = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_KEYWORD_WINDOW = 40
_ENTROPY_THRESHOLD = 4.0


@dataclass(frozen=True)
class EgressText:
    text: str
    counts: dict[str, int] = field(default_factory=dict)
    blocked: bool = False
    reason: str | None = None
    truncated: bool = False


@dataclass(frozen=True)
class EgressValue:
    value: Any
    counts: dict[str, int] = field(default_factory=dict)
    blocked: bool = False
    reason: str | None = None
    truncated: bool = False


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    total = len(value)
    return -sum((n / total) * math.log2(n / total) for n in Counter(value).values())


def _identifier_like(segment: str) -> bool:
    """True for camelCase/PascalCase/lowercase identifiers built from word-sized chunks.

    A ``/``-separated run is a path when every part is identifier-like or a short
    number (``org/3/library/subprocess``); random base64 parts are not.
    """
    if "/" in segment:
        parts = [part for part in segment.split("/") if part]
        return bool(parts) and all((p.isdigit() and len(p) <= 4) or _identifier_like(p) for p in parts)
    if not segment.isalnum():
        return False
    chunks = _CAMEL_CHUNK.findall(segment)
    if "".join(chunks) != segment:
        return False
    digit_chunks = [c for c in chunks if c.isdigit()]
    alpha_chunks = [c for c in chunks if not c.isdigit()]
    single_letters = sum(1 for c in alpha_chunks if len(c) == 1)
    return (
        bool(alpha_chunks)
        and all(len(c) <= 15 for c in alpha_chunks)
        and single_letters <= 2
        and len(digit_chunks) <= 2
        and all(len(c) <= 4 for c in digit_chunks)
    )


def _character_classes(segment: str) -> int:
    return sum(
        (
            any(c.islower() for c in segment),
            any(c.isupper() for c in segment),
            any(c.isdigit() for c in segment),
            any(not c.isalnum() for c in segment),
        )
    )


def _opaque_label(text: str, start: int, segment: str) -> str | None:
    if _identifier_like(segment):
        return None
    window = _MARKER.sub(" ", text[max(0, start - _KEYWORD_WINDOW):start])
    if _SECRET_KEYWORD.search(window):
        return "keyword_token"
    if _character_classes(segment) >= 2 and shannon_entropy(segment) >= _ENTROPY_THRESHOLD:
        return "high_entropy"
    return None


def _redact_opaque_tokens(text: str, counts: Counter[str]) -> str:
    spans = [(m.start(), m.end()) for m in _URL_SPAN.finditer(text)]
    pieces: list[str] = []
    cursor = 0

    def scrub(chunk: str, offset: int, pattern: re.Pattern[str]) -> str:
        def replace(match: re.Match[str]) -> str:
            label = _opaque_label(text, offset + match.start(), match.group(0))
            if label is None:
                return match.group(0)
            counts[label] += 1
            return f"[REDACTED:{label}]"

        return pattern.sub(replace, chunk)

    for start, end in spans:
        pieces.append(scrub(text[cursor:start], cursor, _TOKEN_OUTSIDE_URL))
        pieces.append(scrub(text[start:end], start, _TOKEN_INSIDE_URL))
        cursor = end
    pieces.append(scrub(text[cursor:], cursor, _TOKEN_OUTSIDE_URL))
    return "".join(pieces)


def _redact_short_credentials(text: str, counts: Counter[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        value = match.group("value")
        if not any(c.isdigit() for c in value) or value.startswith("[REDACTED"):
            return match.group(0)
        counts["credential"] += 1
        start = match.start("value") - match.start()
        return match.group(0)[:start] + "[REDACTED:credential]"

    return _SHORT_CREDENTIAL.sub(replace, text)


def _surviving_credentials(text: str) -> list[str]:
    findings = set(scan_text_for_findings(text))
    decoded = urllib.parse.unquote(text)
    if decoded != text:
        findings.update(scan_text_for_findings(decoded))
    return sorted(findings & CREDENTIAL_FINDINGS)


def prepare_egress(text: object, *, max_chars: int = MAX_FIELD_CHARS) -> EgressText:
    """Redact one field for egress; ``blocked`` means the request must not be sent."""
    raw = "" if text is None else text if isinstance(text, str) else str(text)
    limit = max(int(max_chars), 0)
    # Only the head can be sent; scanning a bounded window (with slack so a secret
    # straddling the cap is still seen whole) keeps huge inputs cheap.
    window = limit * 2 + 512
    truncated = len(raw) > limit
    if len(raw) > window:
        # Never scan a value cut in half: once earlier blobs shrink to markers a
        # fragment too short to look like a secret (or an email/IP) would be sent.
        head = raw[:window]
        cut = len(head)
        while cut > 0 and not head[cut - 1].isspace():
            cut -= 1
        if cut == 0:
            # No whitespace to cut at: the straddling value cannot be separated from
            # the rest of the run, so the whole unscannable tail is dropped, marked.
            return EgressText(TRUNCATED_MARKER, {"truncated_value": 1}, False, None, True)
        raw = head[:cut]
    counts: Counter[str] = Counter()
    out = _redact_encoded_home_paths(raw, counts)
    for label, pattern, replacement in _RULES:
        before_rule = out
        out, n = pattern.subn(replacement, out)
        if n and label == "phone":  # a candidate with the wrong digit count is kept, not counted
            n = out.count("[REDACTED:phone]") - before_rule.count("[REDACTED:phone]")
        if n:
            counts[label] += n
    before_core = out
    out, core_findings = redact_text(before_core)
    for name in core_findings:
        added = out.count(f"[REDACTED:{name}]") - before_core.count(f"[REDACTED:{name}]")
        counts[name] += max(added, 1)
    out = _redact_short_credentials(out, counts)
    out = _redact_opaque_tokens(out, counts)
    surviving = _surviving_credentials(out)
    if surviving:
        return EgressText("", dict(counts), True, "credential_survived:" + ",".join(surviving), False)
    truncated = truncated or len(out) > limit
    return EgressText(out[:limit], dict(counts), False, None, truncated)


def prepare_egress_value(value: Any, *, max_chars: int = MAX_FIELD_CHARS) -> EgressValue:
    """Apply :func:`prepare_egress` to every string inside a JSON-like value.

    Mapping keys are code-defined and kept verbatim.  If any string is blocked the
    whole value is withheld (``value=None``).
    """
    counts: Counter[str] = Counter()
    reasons: list[str] = []
    truncated = False

    def walk(node: Any) -> Any:
        nonlocal truncated
        if isinstance(node, str):
            result = prepare_egress(node, max_chars=max_chars)
            counts.update(result.counts)
            truncated = truncated or result.truncated
            if result.blocked and result.reason:
                reasons.append(result.reason)
            return result.text
        if isinstance(node, Mapping):
            return {key: walk(item) for key, item in node.items()}
        if isinstance(node, (list, tuple)):
            return [walk(item) for item in node]
        return node

    redacted = walk(value)
    if reasons:
        names = sorted({name for reason in reasons for name in reason.split(":", 1)[1].split(",")})
        return EgressValue(None, dict(counts), True, "credential_survived:" + ",".join(names), truncated)
    return EgressValue(redacted, dict(counts), False, None, truncated)


__all__ = [
    "CREDENTIAL_FINDINGS",
    "EgressText",
    "EgressValue",
    "MAX_FIELD_CHARS",
    "prepare_egress",
    "prepare_egress_value",
    "shannon_entropy",
]
