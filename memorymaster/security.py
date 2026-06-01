from __future__ import annotations

import base64
import binascii
import json
import os
import re
import unicodedata
from collections.abc import Iterator
from collections.abc import Mapping
from dataclasses import asdict
from dataclasses import dataclass

from memorymaster.models import CitationInput, Claim

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # API keys by vendor
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_sts_key", re.compile(r"\bASIA[0-9A-Z]{16}\b")),
    # Stripe keys (sk/rk/pk + live/test).
    ("stripe_key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    # GitHub — all token prefixes: ghp (personal), gho (oauth), ghu (user),
    # ghs (server-to-server), ghr (refresh), github_pat_ (fine-grained).
    # Both patterns report as "github_token" so downstream callers get a
    # single canonical finding label.
    # v2-refresh (oauth_db_row): bumped {36} -> {36,} so synthetic/longer
    # tokens embedded in SQL dumps and CSV exports still match — {36} with
    # a trailing \b missed any token whose body exceeded the canonical length.
    ("github_token", re.compile(
        r"\b(?:(?i:github[_-]?token)\s*=\s*)?(ghp_|gho_|ghu_|ghs_|ghr_)[A-Za-z0-9]{36,}\b"
    )),
    ("github_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    # Slack
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    # Telegram bot tokens (<numeric_id>:<token>)
    ("telegram_bot_token", re.compile(r"\b\d{8,}:[A-Za-z0-9_\-]{30,}\b")),
    # Bearer / JWT / private key blocks. JWT header min 16 chars post-`eyJ`
    # catches real minimum `{"alg":"HS256"}` -> eyJhbGciOiJIUzI1NiJ9.
    ("jwt_token", re.compile(r"\beyJ[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*\b")),
    # v2-refresh (product_copy): require value to contain a digit, underscore,
    # hyphen, or dot — plain English words after 'Bearer' (e.g. "Bearer
    # authentication and will prompt") now pass. Real bearer tokens always
    # contain digits or structural chars.
    ("bearer_token", re.compile(
        r"(?i)\b(?:bearer\s+|bearer\s*:\s*|bearer_token\s*=\s*)"
        r"(?=[A-Za-z0-9_\-\.]*[0-9_\-\.])[A-Za-z0-9_\-\.]{8,}"
    )),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # NOTE: Private IPv4 (10/8, 172.16/12, 192.168/16) is intentionally NOT
    # filtered here. Private IPs appear legitimately in infrastructure claims
    # (e.g. "server IP is 10.0.0.1") and redacting them at ingest time makes
    # claims meaningless. The export-time filter in dream_bridge.py catches
    # private IPs via _DREAM_EXTRA_PATTERNS when seeding to external memory.
    # Database / broker URLs with embedded passwords.
    # Allows empty user (Redis common shape: redis://:password@host) via `[^:\s/@]*:`.
    ("db_url_password", re.compile(
        r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|amqp|amqps)://"
        r"[^:\s/@]*:[^@\s]+@[^\s]+"
    )),
    # Key=value assignments in plain text
    ("password_assignment", re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*([^\s,;]+)")),
    ("token_assignment", re.compile(r"(?i)\b(token|api[_-]?key|secret)\s*[:=]\s*([^\s,;]+)")),
    # Hex tokens (API keys, session tokens) — 40+ hex chars in backticks or after labels
    ("hex_token", re.compile(r"`([0-9a-f]{40,})`")),
    ("hex_token_ctx", re.compile(r"(?i)(?:token|key|secret|credential).{0,80}?([0-9a-f]{40,})")),
    # Markdown credential patterns: **Pass**: value, **Password**: value, etc.
    ("markdown_credential", re.compile(r"(?i)\*\*(?:pass(?:word)?|pwd|secret|token|key|credential)s?\*\*\s*[:=]\s*`?([^\s`,;\n]+)")),
    # Inline backtick credentials after label: `TOKEN`: `value` or TOKEN: `value`
    ("inline_credential", re.compile(r"(?i)(?:_?(?:api_?)?(?:token|key|secret|password|credential)s?_?)`?\s*[:=]\s*`([^`]+)`")),
    # SSH/connection strings with embedded passwords (legacy pattern — kept for
    # non-standard shapes the structured db_url_password above might miss)
    ("connection_password", re.compile(r"(?i)(?:ssh|ftp|mysql|postgres|redis|mongo).*(?:password|pass|pwd)\s*[:=]\s*([^\s,;]+)")),
    # Compound credential identifiers — AUTH_TOKEN=, MY_PASSWD=, PGPASSWORD=,
    # DB_PASSWORD=, CLIENT_SECRET=, API_KEY=, etc. The \bpassword\b boundary
    # fails on these because `_` is a regex word-char; this pattern matches
    # an optional prefix + credential core + optional suffix + value.
    ("compound_credential", re.compile(
        r"(?i)(?:[A-Z][A-Z0-9]*_)?"
        r"(?:password|passwd|pwd|secret|token|api[_-]?key|client_secret|auth_token)"
        r"[A-Z0-9_]*\s*[:=]\s*['\"`]?([^\s'\"`,;]+)"
    )),
    # Abbreviated password keys: MYPW, UPW, ADMINPW, DBPW (all-caps + PW tail).
    # Requires explicit assignment, won't match PWA/PWS/SPWN.
    ("shell_abbr_password", re.compile(r"\b[A-Z]{1,10}PW\s*=\s*['\"`]?([^\s'\"`,;]{3,})")),
    ("sshpass_flag", re.compile(r"(?i)\bsshpass\s+-p\s+(\S+)")),
    # MySQL inline `-p<pw>` (no space), >=6 char value to avoid short flags.
    ("mysql_inline_password", re.compile(r"(?i)(?:^|\s)mysql\s+[^\s|]*-p([A-Za-z0-9!@#$%^&*_\-+=]{6,})")),
    # Prose credential leak: marker word (password/passwd/pin/secret/
    # passphrase/sshpw/api_key) within 80 chars of a password-shaped token
    # (10+ chars, both uppercase and digit). Catches:
    #   "the password is Str0ngPasswordOverHere77"
    #   "PASSWORD for staging is LongFakePwProse_2026"
    ("prose_password", re.compile(
        r"(?i)(?:password|passwd|passphrase|sshpw|\bpin\b|\bsecret\b|api[_-]?key)"
        r"[^\n]{0,80}?"
        r"(?<![A-Za-z0-9_])("
        r"(?=[A-Za-z0-9_!@#$%^&*.\-+=]*[A-Z])"
        r"(?=[A-Za-z0-9_!@#$%^&*.\-+=]*[0-9])"
        r"[A-Za-z][A-Za-z0-9_!@#$%^&*.\-+=]{9,})"
        r"(?![A-Za-z0-9_])"
    )),
    # v2-refresh (private_ip_port_prose): private IPv4 paired with ':<port>'
    # in prose leaks internal topology. Bare private IPs are still allowed
    # through (see the comment above db_url_password) — only the IP+port
    # combination is redacted.
    ("private_ip_port", re.compile(
        r"\b(?:"
        r"10\.(?:\d{1,3}\.){2}\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3}"
        r"):\d{1,5}\b"
    )),
    # v2-refresh (home_path_windows): C:\Users\<name>\... reveals the user's
    # Windows account name. We match the first path segment (1-40 chars) and
    # redact it; downstream paths beyond the username are not re-redacted.
    ("home_path_windows", re.compile(r"[Cc]:\\Users\\([A-Za-z][A-Za-z0-9._\-]{1,40})")),
    # v2-refresh (home_path_unix): /home/<name>/ or /Users/<name>/ likewise
    # exposes usernames. Anchored to start-of-string, whitespace, or a few
    # common surrounding chars so we don't match arbitrary substrings.
    ("home_path_unix", re.compile(
        r"(?:^|[\s=(\"'])((?:/home|/Users)/[a-z][a-z0-9._\-]{1,40}/)"
    )),
    # v2-refresh (card_number_prose): PAN-shaped runs (Visa/MC/Amex BIN +
    # 12-15 more digits, optionally group-separated). Synthetic test values
    # like 4242-4242-4242-4242 and 5555 5555 5555 4444 are still caught.
    ("card_number_pan", re.compile(
        r"\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2})[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b"
    )),
]


def redact_text(text: str) -> tuple[str, list[str]]:
    """Public API: redact secrets from arbitrary text.

    Returns (redacted_text, list_of_finding_names). Use this instead of
    defining local regexes in downstream modules — the patterns here are
    the single source of truth.
    """
    return _redact(text)

_ENCRYPTION_ENV_VAR = "MEMORYMASTER_ENCRYPTION_KEY"
_SENSITIVE_BYPASS_ENV_VAR = "MEMORYMASTER_ALLOW_SENSITIVE_BYPASS"
_SENSITIVE_BYPASS_CONFIG_KEYS = (
    "allow_sensitive_bypass",
    "allow_sensitive_access",
    "sensitive_bypass_enabled",
)
_TRUTHY_VALUES = {"1", "true", "yes", "on", "y"}
_FALSY_VALUES = {"0", "false", "no", "off", "n"}


@dataclass(slots=True)
class SanitizedClaimInput:
    text: str
    object_value: str | None
    citations: list[CitationInput]
    is_sensitive: bool
    findings: list[str]
    encrypted_payload: str | None
    subject: str | None = None
    predicate: str | None = None


def _as_bool(value: object, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUTHY_VALUES:
            return True
        if normalized in _FALSY_VALUES:
            return False
    raise ValueError(f"{field} must be a boolean-like value.")


def _sensitive_bypass_from_config(config: Mapping[str, object] | None) -> bool | None:
    if config is None:
        return None
    targets: list[Mapping[str, object]] = [config]
    nested_security = config.get("security")
    if isinstance(nested_security, Mapping):
        targets.append(nested_security)
    for target in targets:
        for key in _SENSITIVE_BYPASS_CONFIG_KEYS:
            if key in target:
                return _as_bool(target[key], field=f"config.{key}")
    return None


def is_sensitive_bypass_enabled(config: Mapping[str, object] | None = None) -> bool:
    configured = _sensitive_bypass_from_config(config)
    if configured is not None:
        return configured
    raw = os.getenv(_SENSITIVE_BYPASS_ENV_VAR, "")
    return raw.strip().lower() in _TRUTHY_VALUES


def resolve_allow_sensitive_access(
    *,
    allow_sensitive: bool,
    context: str,
    config: Mapping[str, object] | None = None,
    deny_mode: str = "raise",
) -> bool:
    if not allow_sensitive:
        return False
    if is_sensitive_bypass_enabled(config):
        return True
    message = (
        f"{context}: allow_sensitive access denied. "
        f"Set {_SENSITIVE_BYPASS_ENV_VAR}=1 or enable a security config override."
    )
    if deny_mode == "filter":
        return False
    if deny_mode == "raise":
        raise PermissionError(message)
    raise ValueError("deny_mode must be 'raise' or 'filter'.")


# Placeholder tokens from RFC examples, tutorials, docs. If every match a
# pattern makes is a placeholder, we suppress that pattern's finding so docs
# don't get flagged as real credentials.
# v2-refresh (placeholder_tutorial): added optional WORD segment between
# YOUR_ and HERE so shapes like YOUR_STRIPE_KEY_HERE and YOUR_CLIENT_SECRET_HERE
# are recognised as tutorial placeholders. Also added ${VAR}, {{ expr }}, and
# $VAR shell/template interpolations (v2 dollar_variable_reference).
_PLACEHOLDER_MARKERS: re.Pattern[str] = re.compile(
    r"(?i)\bYOUR[_\-]?(?:[A-Z]+[_\-])?(?:TOKEN|KEY|PASSWORD|SECRET|API[_\-]?KEY)[_\-]?HERE?\b"
    r"|\bYOUR_[A-Z]+\b|<[a-z_\- ]{3,40}>|\bREPLACE[_\- ]?ME\b"
    r"|\bsk-[X]{3,}[A-Za-z0-9]*\b|\bsk-YOUR[_\-]?[A-Z]+\b"
    r"|\bmF_9\.B5f-4\.1JqM\b|:password@|=password\b"
    # Case-sensitive alts — the leading (?i) must be switched off via (?-i:)
    # or shell-style $lowercase would match the uppercase-only $VAR class.
    r"|(?-i:\$\{[A-Za-z_][A-Za-z0-9_]*\}|\{\{[^}]{0,120}\}\}|\$[A-Z][A-Z0-9_]*\b)"
)


def _is_placeholder_match(matched_text: str) -> bool:
    return bool(_PLACEHOLDER_MARKERS.search(matched_text))


# v2-refresh (prose_secret_word, product_copy): patterns whose match can trip
# on prose like "tokens: they live in Vault" or "Bearer authentication". For
# these we also require the captured value to look credential-shaped: at
# least 6 chars AND containing either a digit, a non-alphanumeric char, or an
# uppercase letter. Plain lowercase English words are suppressed.
_STRUCTURED_CRED_FINDINGS: frozenset[str] = frozenset({
    "password_assignment",
    "token_assignment",
    "compound_credential",
    "connection_password",
})


def _is_low_entropy_value(value: str) -> bool:
    """Return True if value looks like an English word, not a credential."""
    if len(value) < 6:
        return True
    has_digit = any(c.isdigit() for c in value)
    has_special = any(not c.isalnum() and c != "_" for c in value)
    has_upper = any(c.isupper() for c in value)
    return not (has_digit or has_special or has_upper)


def _match_is_suppressed(name: str, match: re.Match[str]) -> bool:
    """Return True if a match should be ignored (placeholder / low-entropy)."""
    if _is_placeholder_match(match.group(0)):
        return True
    # v2-refresh (prose_secret_word, product_copy): reject plain-word values
    # for structured credential patterns, which capture a value group.
    if name in _STRUCTURED_CRED_FINDINGS and match.lastindex:
        captured = match.group(match.lastindex)
        if captured and _is_low_entropy_value(captured):
            return True
    return False


def _redact(text: str) -> tuple[str, list[str]]:
    findings: list[str] = []
    out = text
    for name, pattern in _SECRET_PATTERNS:
        matches = list(pattern.finditer(out))
        if not matches:
            continue
        if all(_match_is_suppressed(name, m) for m in matches):
            continue
        findings.append(name)
        # Preserve non-suppressed matches only: substitute match-by-match so
        # a placeholder-only occurrence isn't overwritten by [REDACTED:*].
        def _sub(m: re.Match[str]) -> str:
            return m.group(0) if _match_is_suppressed(name, m) else f"[REDACTED:{name}]"
        out = pattern.sub(_sub, out)
    return out, findings


# --- Shared encoded-secret variant scanner (audit: ingest-encoded-secret) ----
# Previously this base64/hex/confusable decoder lived ONLY in mcp_server and
# ran only on ingest_claim's `text` field, so every other ingest path
# (ingest_rule, dream_bridge, transcript_miner, verbatim_store, service.ingest
# of object_value/subject/predicate/citations) persisted encoded secrets
# verbatim. Moving it into the storage-time chokepoint here means all callers
# that route through `_redact`/`sanitize_claim_input`/`is_sensitive_claim`
# detect an encoded secret and flag the claim sensitive. mcp_server reuses the
# same generator so there is a single source of truth.
_BASE64_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9+/=_-])"
    r"(?:[A-Za-z0-9+/]{20,}={0,2}|[A-Za-z0-9_-]{20,}={0,2})"
    r"(?![A-Za-z0-9+/=_-])"
)
_HEX_ESCAPE_SEQUENCE_RE = re.compile(r"(?:\\x[0-9A-Fa-f]{2}){4,}")
_CONFUSABLE_ASCII_MAP = str.maketrans({
    "а": "a",  # Cyrillic small a
    "А": "A",
    "е": "e",
    "Е": "E",
    "о": "o",
    "О": "O",
    "р": "p",
    "Р": "P",
    "с": "c",
    "С": "C",
    "х": "x",
    "Х": "X",
    "у": "y",
    "У": "Y",
    "і": "i",
    "І": "I",
})
_MAX_SECRET_SCAN_VARIANTS = 64


def _add_scan_variant(queue: list[str], seen: set[str], value: str) -> None:
    if not value or value in seen or len(seen) + len(queue) >= _MAX_SECRET_SCAN_VARIANTS:
        return
    seen.add(value)
    queue.append(value)


def _decode_text_bytes(raw: bytes) -> str | None:
    if not raw:
        return None
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\x00" in decoded:
        return None
    printable = sum(char.isprintable() or char in "\r\n\t" for char in decoded)
    return decoded if printable / max(len(decoded), 1) >= 0.85 else None


def _decode_base64_candidate(candidate: str) -> str | None:
    if len(candidate) % 4 == 1:
        return None
    padded = candidate + ("=" * (-len(candidate) % 4))
    try:
        return _decode_text_bytes(base64.b64decode(padded, validate=True))
    except binascii.Error:
        try:
            return _decode_text_bytes(base64.urlsafe_b64decode(padded))
        except (binascii.Error, ValueError):
            return None


def _decode_hex_escape_sequence(candidate: str) -> str | None:
    raw = bytes(int(pair, 16) for pair in re.findall(r"\\x([0-9A-Fa-f]{2})", candidate))
    return _decode_text_bytes(raw)


def _iter_json_scan_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_json_scan_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
                if isinstance(item, (str, int, float, bool)):
                    yield f"{key}={item}"
            yield from _iter_json_scan_strings(item)


def _extract_json_scan_strings(text: str) -> Iterator[str]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        yield from _iter_json_scan_strings(value)


def expand_secret_scan_variants(text: str) -> Iterator[str]:
    """Yield ``text`` plus decoded/normalized variants for secret scanning.

    Expands confusable-folded, base64-decoded, hex-escape-decoded, and embedded
    JSON-string variants (breadth-first, capped at ``_MAX_SECRET_SCAN_VARIANTS``)
    so a credential hidden behind one layer of encoding is still surfaced to the
    regex filter. This is the single source of truth shared by the storage-time
    chokepoint and the MCP ingest guard.
    """
    if not text:
        return
    seen = {text}
    queue = [text]
    while queue:
        current = queue.pop(0)
        yield current

        normalized = unicodedata.normalize("NFKC", current).translate(_CONFUSABLE_ASCII_MAP)
        _add_scan_variant(queue, seen, normalized)

        for match in _HEX_ESCAPE_SEQUENCE_RE.finditer(current):
            decoded = _decode_hex_escape_sequence(match.group(0))
            if decoded:
                _add_scan_variant(queue, seen, decoded)

        for match in _BASE64_CANDIDATE_RE.finditer(current):
            decoded = _decode_base64_candidate(match.group(0))
            if decoded:
                _add_scan_variant(queue, seen, decoded)

        for nested in _extract_json_scan_strings(current):
            _add_scan_variant(queue, seen, nested)


def scan_text_for_findings(text: str) -> list[str]:
    """Return de-duplicated finding names across all encoded variants of ``text``.

    Unlike ``_redact`` (which substitutes in-place on the literal text only),
    this walks decoded/normalized variants so an encoded secret is detected even
    though it cannot be substituted back into the original bytes. Callers use the
    result to decide whether a claim is sensitive (flag + encrypt-at-rest).
    """
    findings: list[str] = []
    for variant in expand_secret_scan_variants(text):
        _, variant_findings = _redact(variant)
        for finding in variant_findings:
            if finding not in findings:
                findings.append(finding)
    return findings


def _get_fernet():
    key = os.getenv(_ENCRYPTION_ENV_VAR)
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Encryption key was provided but cryptography is not installed. "
            "Install with: pip install 'memorymaster[security]'"
        ) from exc
    return Fernet(key.encode("utf-8"))


def _encrypt_payload(payload: dict[str, object]) -> str | None:
    fernet = _get_fernet()
    if fernet is None:
        return None
    raw = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    return base64.urlsafe_b64encode(fernet.encrypt(raw)).decode("utf-8")


def sanitize_claim_input(
    *,
    text: str,
    object_value: str | None,
    citations: list[CitationInput],
    subject: str | None = None,
    predicate: str | None = None,
) -> SanitizedClaimInput:
    redacted_text, findings = _redact(text)
    redacted_object = object_value
    object_findings: list[str] = []
    if object_value:
        redacted_object, object_findings = _redact(object_value)
        findings.extend(object_findings)

    # subject/predicate are structured-claim fields that reach the store
    # alongside text/object_value. They are exposed MCP ingest parameters, so a
    # secret placed there must be caught by the ingest filter — the last line of
    # defense — not only at display time. (audit: ingest-subject-skips-filter)
    redacted_subject = subject
    if subject:
        redacted_subject, subject_findings = _redact(subject)
        findings.extend(subject_findings)
    redacted_predicate = predicate
    if predicate:
        redacted_predicate, predicate_findings = _redact(predicate)
        findings.extend(predicate_findings)

    sanitized_citations: list[CitationInput] = []
    citation_findings: list[str] = []
    for cite in citations:
        excerpt = cite.excerpt
        if excerpt:
            excerpt, c_findings = _redact(excerpt)
            citation_findings.extend(c_findings)
        sanitized_citations.append(CitationInput(source=cite.source, locator=cite.locator, excerpt=excerpt))
    findings.extend(citation_findings)

    # Encoded-secret sweep (audit: ingest-encoded-secret): a credential hidden
    # behind base64/hex/confusable encoding survives the literal `_redact`
    # substitution above (the regexes don't match the encoded bytes). Scan the
    # decoded variants of every inbound field so the claim is flagged sensitive
    # (and thus encrypted-at-rest / hidden from recall) even when the raw text
    # we persist still carries the encoded form.
    for raw_field in (text, object_value, subject, predicate):
        if raw_field:
            findings.extend(scan_text_for_findings(raw_field))
    for cite in citations:
        if cite.excerpt:
            findings.extend(scan_text_for_findings(cite.excerpt))

    dedup_findings = sorted(set(findings))
    is_sensitive = len(dedup_findings) > 0
    encrypted_payload = _encrypt_payload(
        {
            "text": text,
            "object_value": object_value,
            "subject": subject,
            "predicate": predicate,
            "citations": [asdict(c) for c in citations],
        }
    ) if is_sensitive else None

    return SanitizedClaimInput(
        text=redacted_text,
        object_value=redacted_object,
        citations=sanitized_citations,
        is_sensitive=is_sensitive,
        findings=dedup_findings,
        encrypted_payload=encrypted_payload,
        subject=redacted_subject,
        predicate=redacted_predicate,
    )


def is_sensitive_claim(claim: Claim) -> bool:
    combined = " ".join(
        part for part in [claim.text, claim.object_value or "", claim.subject or "", claim.predicate or ""] if part
    )
    if "[REDACTED:" in combined:
        return True
    # Scan decoded variants too so a stored claim carrying an encoded secret
    # (base64/hex/confusable) is still treated as sensitive at read time.
    return len(scan_text_for_findings(combined)) > 0
