from __future__ import annotations

import base64
import binascii
import json
import os
import re
import unicodedata
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Mapping
from dataclasses import asdict
from dataclasses import dataclass

from memorymaster.core.models import CitationInput, Claim

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # API keys by vendor
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    # S-1: 40-char AWS secret access key. A bare 40-char token is too common (git SHAs are 40 hex), so a match
    # only counts with AWS context (_aws_secret_has_context). It runs BEFORE aws_access_key so the key id that
    # supplies the context is still in the text.
    ("aws_secret_key", re.compile(
        r"(?<![A-Za-z0-9/+=])(?=[A-Za-z0-9/+]{0,40}[A-Z])(?=[A-Za-z0-9/+]{0,40}[a-z])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=])"
    )),
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
    # S-1: scheme://user:password@host for any scheme (https, ftp, smb, mqtt, rtsp...). Only the password span
    # (group "secret") is redacted, user and host stay readable. '/' ends the password, so ports and paths pass.
    ("url_basic_auth_password", re.compile(
        r"(?i)\b[a-z][a-z0-9+.\-]{1,15}://(?!\[REDACTED)[^:\s/@]{0,64}:(?!\[REDACTED)(?P<secret>[^\s/]{1,256})@"
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
        # Linear time: the prefix only starts at a word start and the suffix is bounded, so a long
        # alphanumeric run or "tokentokentoken..." cannot make it quadratic.
        r"(?i)(?:(?<![A-Za-z0-9])[A-Z][A-Z0-9]*_)?"
        r"(?:password|passwd|pwd|secret|token|api[_-]?key|client_secret|auth_token)"
        r"[A-Z0-9_]{0,64}\s*[:=]\s*['\"`]?([^\s'\"`,;]+)"
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
        r"(?<![A-Za-z0-9_])"
        # Exempt only a lowercase config shape (min_length=12, hashing=bcrypt2) that does not follow a
        # connector: "the secret is Abc=123def4567" / "reset to Abc=Def123456" are still passwords.
        r"(?:(?<=\bis )|(?<=\bto )|(?<=\bes )|(?<=:)|(?<=: )|(?<=\n)"
        r"|(?![a-z_][a-z_.]{0,30}=[a-z0-9_.\-]{1,16}(?![A-Za-z0-9])))("
        r"(?=[A-Za-z0-9_!@#$%^&*.\-+=]*[A-Z])"
        r"(?=[A-Za-z0-9_!@#$%^&*.\-+=]*[0-9])"
        r"[A-Za-z][A-Za-z0-9_!@#$%^&*.\-+=]{9,})"
        r"(?![A-Za-z0-9_])"
    )),
    # S-1: Spanish password prose ("la contraseña del servidor es X", "clave: X", "usuario admin clave X").
    # Keyword: contraseña/clave (a connector es/son/era/fue/:/= after up to 4 plain words, or the value directly)
    # or English password/passwd/pwd (connector REQUIRED; English prose is prose_password's job). The bare verb
    # "pass" is not a keyword. Compounds (clave primaria/foránea/pública/privada/única/compuesta/de api/de
    # cache/caché) are not passwords. The value is one whole token of 6-64 chars (quoted or not) and is judged
    # by _prose_secret_is_credible. Every run is bounded so nothing here is quadratic. Only the value is redacted.
    ("password_prose_es", re.compile(
        r"(?i)\b(?:"
        r"(?:contrase[ñn]as?|claves?)\b"
        r"(?!\s{1,8}(?:primaria|for[aá]nea|p[uú]blica|privada|[uú]nica|compuesta"
        r"|de\s{1,8}(?:la\s{1,8})?(?:api|cach[eé])|del\s{1,8}cach[eé])\b)"
        r"(?:(?:\s{1,8}[\w.\-]{1,40}){0,4}?(?:\s{1,8}(?:es|son|era|fue)\b|\s{0,8}[:=])|\s{1,8})"
        r"|(?:password|passwd|pwd)\b"
        r"(?:(?:\s{1,8}[\w.\-]{1,40}){0,4}?(?:\s{1,8}(?:is|es|son|era|fue)\b|\s{0,8}[:=]))"
        r")"
        r"[ \t]{0,8}(?!\[REDACTED)"
        r"(?P<q>[`'\"])?"
        r"(?P<secret>[^\s`'\",;]{6,64})(?![^\s`'\",;])"
    )),
    # S-1: one-time code / PIN, only with an explicit keyword right before 4-8 digits ("OTP 493021",
    # "código de verificación: 493021", "el pin es 4821"). Bare numbers never match. Only the digits are redacted.
    ("otp_pin", re.compile(
        r"(?i)\b(?:otp|pin|c[oó]digo\s{1,8}(?:de\s{1,8})?(?:verificaci[oó]n|seguridad)"
        r"|(?:verification|security|one[- ]time)\s{1,8}(?:code|password))\b"
        r"(?:\s{1,8}(?:is|es|code|c[oó]digo|n[uú]mero|number|was|ser[aá])){0,2}"
        r"(?:\s{0,8}[:=])?\s{0,8}[`'\"]?(?<![\d.])(?P<secret>\d{4,8})(?!\d|[.,]\d)"
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
    # S-8: both slash styles, and the Git Bash spelling /c/Users/<name>. "Users" stays case-sensitive: the lowercase
    # spellings are the egress absolute-path rules' job (they redact the whole path, this one only the prefix).
    ("home_path_windows", re.compile(
        r"(?:[Cc]:[\\/]|(?<![\w/])/[Cc]/)Users[\\/]([A-Za-z][A-Za-z0-9._\-]{1,40})"
    )),
    # v2-refresh (home_path_unix): /home/<name>/ or /Users/<name>/ likewise
    # exposes usernames. Anchored to start-of-string, whitespace, or a few
    # common surrounding chars so we don't match arbitrary substrings.
    ("home_path_unix", re.compile(
        r"(?:^|[\s=(\"'])((?:/home|/Users)/[A-Za-z][A-Za-z0-9._\-]{1,40}/)"
    )),
    # v2-refresh (card_number_prose): PAN-shaped runs (Visa/MC/Amex BIN +
    # 12-15 more digits, optionally group-separated). Synthetic test values
    # like 4242-4242-4242-4242 and 5555 5555 5555 4444 are still caught.
    ("card_number_pan", re.compile(
        r"\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2})[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b"
    )),
    # S-7: CUIT/CUIL, dashed (NN-NNNNNNNN-N) or an 11-digit run (not glued to letters: hex digests carry digit runs), prefix 20/23/24/27/30/33/34, and only when the
    # mod-11 check digit validates (_cuit_check_digit_ok), so phones, timestamps and ids stay readable.
    ("argentine_cuit_cuil", re.compile(
        r"(?<![A-Za-z0-9\-.])(?:(?:20|23|24|27|30|33|34)-\d{8}-\d|(?:20|23|24|27|30|33|34)\d{9})(?![A-Za-z0-9]|-\d|\.\d)"
    )),
    # T-2274 argentine_dni: Argentine national id, only with a document keyword in front
    # (DNI / D.N.I. / Documento [Nacional de Identidad] [N° / nro]) and 7-8 digits, dotted or not.
    # shortcut: a bare 12.345.678 stays readable (it is indistinguishable from an ARS amount);
    # upgrade only with a context-free signal.
    ("argentine_dni", re.compile(
        r"(?i)\b(?:d\.?\s?n\.?\s?i\.?|documento(?:\s+nacional\s+de\s+identidad)?)"
        r"(?:\s+(?:del\s+(?:cliente|titular|usuario)\s+)?(?:es|is))?"
        r"(?:\s*(?:n[°º]|nro\.?|n[úu]mero|n\.?))?\s*[:#\-]?\s*"
        r"\d{1,2}[. ]?\d{3}[. ]?\d{3}\b"
    )),
    # T-2274 base64_secret: a standalone base64 blob that no vendor pattern names (an encoded password,
    # key material, a wrapped token). Needs upper + lower + digit so hex hashes, identifiers and prose
    # pass; padded blobs from 24 chars, unpadded from 40 and without '/', so file paths do not match.
    # '_' '-' '=' glued to the blob block the match (sha512-... integrity tags, key=value).
    # An SSH public key line (`ssh-ed25519 <blob>`, `ssh-rsa <blob>`, ...) is not a secret: exempt by the
    # key-type word right before the blob (fixed-width lookbehinds, one per type).
    ("base64_secret", re.compile(
        r"(?<!ssh-ed25519 )(?<!ssh-rsa )(?<!ssh-dss )"
        r"(?<!ecdsa-sha2-nistp256 )(?<!ecdsa-sha2-nistp384 )(?<!ecdsa-sha2-nistp521 )"
        r"(?<!SHA256:)(?<!sha256:)"  # an OpenSSH key fingerprint (SHA256:<43 b64>) is public
        r"(?<![A-Za-z0-9+/=_\-])"
        r"(?=[A-Za-z0-9+/]*[A-Z])(?=[A-Za-z0-9+/]*[a-z])(?=[A-Za-z0-9+/]*[0-9])"
        r"(?:[A-Za-z0-9+/]{24,}={1,2}|[A-Za-z0-9+]{40,})"
        r"(?![A-Za-z0-9+/=_\-])"
    )),
]

# Claim memory has a stricter contract than user-selected raw source/evidence:
# local topology and machine-specific absolute paths are not durable memories.
# Keep these out of _SECRET_PATTERNS so ADR-0006 raw source preservation stays
# unchanged while every SQLite/Postgres/service/spool claim writer is covered
# by sanitize_claim_input.
_CLAIM_ONLY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_ipv4",
        re.compile(
            r"\b(?:"
            r"10\.(?:\d{1,3}\.){2}\d{1,3}"
            r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
            r"|192\.168\.\d{1,3}\.\d{1,3}"
            r")\b"
        ),
    ),
    (
        "absolute_path_windows",
        re.compile(
            r"(?i)(?<![A-Za-z0-9_])[A-Z]:[\\/](?![\\/])"
            r"(?:[^\s\"'<>|`\r\n]|[ ](?=[^\s]*[\\/]))+"
        ),
    ),
    (
        "absolute_path_unc",
        re.compile(
            r"(?i)(?<!\\)\\\\[^\\\s]+\\"
            r"(?:[^\s\"'<>|`\r\n]|[ ](?=[^\s]*[\\/]))+"
        ),
    ),
)

# Loopback and link-local addresses intentionally remain readable: they identify
# the current host/link, not private fleet topology. CGNAT and IPv6 ULA policy is
# separate from this RFC1918-specific intake rule.


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


@dataclass(slots=True)
class SanitizedClaimStructureInput:
    claim_type: str | None
    subject: str | None
    predicate: str | None
    object_value: str | None
    findings: list[str]


@dataclass(slots=True)
class SanitizedEventInput:
    details: str | None
    payload: object
    findings: list[str]


class SensitiveMetadataError(ValueError):
    """Fail-closed metadata rejection that never echoes the supplied value."""

    def __init__(self, field: str, findings: list[str]) -> None:
        self.field = field
        self.findings = tuple(sorted(set(findings)))
        labels = ", ".join(self.findings)
        super().__init__(f"{field} contains sensitive data ({labels})")


def normalize_sensitivity_findings(findings: Iterable[str] | None) -> list[str]:
    """Validate and canonicalize findings-only security metadata."""
    if findings is None:
        return []
    if isinstance(findings, (str, bytes)):
        raise ValueError("Sensitivity findings must be a collection of labels.")
    normalized: set[str] = set()
    for value in findings:
        if not isinstance(value, str) or re.fullmatch(r"[a-z0-9_]{1,64}", value) is None:
            raise ValueError("Invalid sensitivity finding label.")
        normalized.add(value)
    return sorted(normalized)


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


_SHA_DIGEST_CONTEXT = re.compile(r"(?i)\bsha[-_ ]?(?:256|512)\s*[:=]?\s*$")


_AWS_KEY_ID_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_AWS_CONTEXT_RE = re.compile(r"(?i)(?<![a-z])aws|secret[_ \-]?access[_ \-]?key")
_CUIT_WEIGHTS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)


_aws_id_seen: tuple[str, bool] = ("", False)


def _text_has_aws_key_id(text: str) -> bool:
    """Search once per text object (a 300 KB text with thousands of candidates must not rescan per match)."""
    global _aws_id_seen
    if _aws_id_seen[0] is not text:
        _aws_id_seen = (text, bool(_AWS_KEY_ID_RE.search(text)))
    return _aws_id_seen[1]


def _aws_secret_has_context(match: re.Match[str]) -> bool:
    """An AWS key id anywhere in the text, or aws / secret access key within 60 chars before the token."""
    return _text_has_aws_key_id(match.string) or bool(
        _AWS_CONTEXT_RE.search(match.string[max(0, match.start() - 60):match.start()])
    )


_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}")
_IDENT_JOIN_RE = re.compile(r"[a-z_][a-z_.]*[+#%@*&^~$!\-]+(?:[a-z_][a-z_.]*|\d{1,3})")
_PROSE_SYMBOLS = frozenset("!@#$%^&*+~")


def _prose_secret_is_credible(value: str, quoted: bool) -> bool:
    """Judge a password_prose_es value: a symbol alone is not evidence, emails/URLs/paths/identifier joins are not."""
    v = value.rstrip(".,;:?)")
    if len(v) < 6 or v[0] in "-/=~" or v.startswith(("./", "../")) or "://" in v or v.startswith("www."):
        return False
    if (len(v) > 2 and v[1] == ":" and v[2] in "\\/") or _EMAIL_RE.fullmatch(v) or _IDENT_JOIN_RE.fullmatch(v):
        return False
    has_digit = any(c.isdigit() for c in v)
    if not (any(c.isalpha() for c in v) and has_digit):
        return False
    if quoted:
        return True
    if any(c in _PROSE_SYMBOLS for c in value):
        return True
    digits = sum(c.isdigit() for c in v)
    return digits >= 2 and len(v) >= 8 and any(c.isupper() for c in v) and any(c.islower() for c in v)


def _cuit_check_digit_ok(raw: str) -> bool:
    digits = raw.replace("-", "")
    check = 11 - sum(int(d) * w for d, w in zip(digits, _CUIT_WEIGHTS)) % 11
    return int(digits[10]) == (0 if check == 11 else check)


def _match_is_suppressed(name: str, match: re.Match[str]) -> bool:
    """Return True if a match should be ignored (placeholder / low-entropy)."""
    if _is_placeholder_match(match.group(0)):
        return True
    if name == "aws_secret_key":
        return not _aws_secret_has_context(match)
    if name == "argentine_cuit_cuil":
        return not _cuit_check_digit_ok(match.group(0))
    if name == "password_prose_es":
        return not _prose_secret_is_credible(match.group("secret"), bool(match.group("q")))
    if name == "base64_secret":
        # A bare sha256 (44 chars, one '=') or sha512 (88 chars, '==') base64 digest named as such
        # right before it is a checksum, not a secret.
        blob = match.group(0)
        if (len(blob), blob.count("=")) in ((44, 1), (88, 2)) and _SHA_DIGEST_CONTEXT.search(
            match.string[max(0, match.start() - 24):match.start()]
        ):
            return True
        # A blob that decodes to a recognised secret is left in place on purpose: every caller
        # that redacts and then rescans (profile/decisions egress, local-search snippets,
        # sanitize_persisted_text) must still see the decoded hit in the redacted text to block
        # or blank it. Masking it here as a plain base64_secret would downgrade that to a redaction.
        decoded = _decode_base64_candidate(match.group(0))
        return bool(decoded) and bool(scan_text_for_findings(decoded))
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
            if _match_is_suppressed(name, m):
                return m.group(0)
            marker = f"[REDACTED:{name}]"
            if "secret" in (m.re.groupindex or {}):  # redact only the secret span, keep the surrounding words
                whole, start = m.group(0), m.start()
                return whole[:m.start("secret") - start] + marker + whole[m.end("secret") - start:]
            return marker
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
# Zero-width characters split a secret so the regexes miss it; scanned as an extra variant (never stripped from
# stored text: U+200D joins emoji sequences). A hit neutralizes the whole field via sanitize_persisted_text.
_ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\ufeff]")


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

        normalized = unicodedata.normalize("NFKC", current)
        if _ZERO_WIDTH_RE.search(normalized):
            normalized = _ZERO_WIDTH_RE.sub("", normalized)
        normalized = normalized.translate(_CONFUSABLE_ASCII_MAP)
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


def _iter_persisted_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str):
                yield key
            yield from _iter_persisted_strings(nested)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for nested in value:
            yield from _iter_persisted_strings(nested)


def scan_persisted_value(value: object) -> list[str]:
    """Scan every string leaf/key in a persisted scalar or JSON-like value."""
    findings: list[str] = []
    for text in _iter_persisted_strings(value):
        for finding in scan_text_for_findings(text):
            if finding not in findings:
                findings.append(finding)
    return findings


def validate_persisted_metadata(fields: Mapping[str, object]) -> None:
    """Reject secret-shaped identifiers/provenance before any durable write."""
    for field, value in fields.items():
        findings = scan_persisted_value(value)
        if findings:
            raise SensitiveMetadataError(field, findings)


def sanitize_persisted_text(text: str) -> tuple[str, list[str]]:
    """Redact literal secrets and neutralize encoded secrets in content fields."""
    redacted, literal_findings = _redact(text)
    findings = sorted(set(literal_findings + scan_text_for_findings(text)))
    if findings and scan_text_for_findings(redacted):
        redacted = "[REDACTED:encoded_secret]"
    return redacted, findings


def _sanitize_memory_claim_text(text: str) -> tuple[str, list[str]]:
    """Apply generic secret filtering plus the stricter claim-memory policy."""
    redacted, findings = sanitize_persisted_text(text)
    for name, pattern in _CLAIM_ONLY_PATTERNS:
        if pattern.search(redacted) is None:
            continue
        redacted = pattern.sub(f"[REDACTED:{name}]", redacted)
        findings.append(name)
    return redacted, sorted(set(findings))


def _structured_context_findings(value: str, context_keys: tuple[str, ...]) -> list[str]:
    independent_findings = scan_persisted_value(value)
    findings: list[str] = []
    for key in context_keys:
        for finding in scan_text_for_findings(f"{key}={value}"):
            if finding not in independent_findings and finding not in findings:
                findings.append(finding)
    return findings


def _sanitize_persisted_json(
    value: object,
    context_keys: tuple[str, ...],
) -> tuple[object, list[str]]:
    if isinstance(value, str):
        sanitized, findings = sanitize_persisted_text(value)
        structured_findings = _structured_context_findings(value, context_keys)
        if structured_findings:
            sanitized = "[REDACTED:structured_secret]"
            findings.extend(structured_findings)
        return sanitized, sorted(set(findings))
    if isinstance(value, (int, float, bool)):
        structured_findings = _structured_context_findings(str(value), context_keys)
        if structured_findings:
            return "[REDACTED:structured_secret]", structured_findings
        return value, []
    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        findings: list[str] = []
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError("Persisted JSON object keys must be strings.")
            validate_persisted_metadata({"payload_json_key": key})
            sanitized_value, nested_findings = _sanitize_persisted_json(
                nested,
                (*context_keys, key),
            )
            sanitized[key] = sanitized_value
            findings.extend(nested_findings)
        return sanitized, sorted(set(findings))
    if isinstance(value, (list, tuple)):
        sanitized_items: list[object] = []
        findings = []
        for nested in value:
            sanitized_value, nested_findings = _sanitize_persisted_json(
                nested,
                context_keys,
            )
            sanitized_items.append(sanitized_value)
            findings.extend(nested_findings)
        return sanitized_items, sorted(set(findings))
    return value, []


def sanitize_persisted_json(value: object) -> tuple[object, list[str]]:
    """Return a recursively sanitized JSON-compatible copy of ``value``."""
    return _sanitize_persisted_json(value, ())


def sanitize_claim_structure_input(
    *,
    claim_type: str | None,
    subject: str | None,
    predicate: str | None,
    object_value: str | None,
) -> SanitizedClaimStructureInput:
    validate_persisted_metadata({"claim_type": claim_type})
    sanitized_subject, subject_findings = _sanitize_optional_memory_claim_text(subject)
    sanitized_predicate, predicate_findings = _sanitize_optional_memory_claim_text(predicate)
    sanitized_object, object_findings = _sanitize_optional_memory_claim_text(object_value)
    findings = sorted(set(subject_findings + predicate_findings + object_findings))
    return SanitizedClaimStructureInput(
        claim_type=claim_type,
        subject=sanitized_subject,
        predicate=sanitized_predicate,
        object_value=sanitized_object,
        findings=findings,
    )


def sanitize_event_input(
    *,
    event_type: str,
    from_status: str | None,
    to_status: str | None,
    details: str | None,
    payload: object,
    created_at: object,
    tenant_id: str | None = None,
) -> SanitizedEventInput:
    validate_persisted_metadata(
        {
            "event_type": event_type,
            "from_status": from_status,
            "to_status": to_status,
            "created_at": created_at,
            "tenant_id": tenant_id,
        }
    )
    sanitized_details, detail_findings = _sanitize_optional_claim_text(details)
    sanitized_payload, payload_findings = sanitize_persisted_json(payload)
    return SanitizedEventInput(
        details=sanitized_details,
        payload=sanitized_payload,
        findings=sorted(set(detail_findings + payload_findings)),
    )


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


def _sanitize_optional_claim_text(value: str | None) -> tuple[str | None, list[str]]:
    if value is None:
        return None, []
    return sanitize_persisted_text(value)


def _sanitize_optional_memory_claim_text(
    value: str | None,
) -> tuple[str | None, list[str]]:
    if value is None:
        return None, []
    return _sanitize_memory_claim_text(value)


def _sanitize_claim_citations(
    citations: list[CitationInput],
) -> tuple[list[CitationInput], list[str]]:
    sanitized: list[CitationInput] = []
    findings: list[str] = []
    for citation in citations:
        excerpt, excerpt_findings = _sanitize_optional_memory_claim_text(citation.excerpt)
        findings.extend(excerpt_findings)
        sanitized.append(CitationInput(citation.source, citation.locator, excerpt))
    return sanitized, findings


def sanitize_claim_input(
    *,
    text: str,
    object_value: str | None,
    citations: list[CitationInput],
    subject: str | None = None,
    predicate: str | None = None,
    idempotency_key: str | None = None,
    claim_type: str | None = None,
    scope: str | None = None,
    volatility: str | None = None,
    source_agent: str | None = None,
    visibility: str | None = None,
    holder: str | None = None,
    confidence: object = None,
    event_time: str | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    intake_batch_id: str | None = None,
    tenant_id: str | None = None,
) -> SanitizedClaimInput:
    citation_metadata = {
        "citation_source": [citation.source for citation in citations],
        "citation_locator": [citation.locator for citation in citations],
    }
    validate_persisted_metadata({
        "idempotency_key": idempotency_key,
        "claim_type": claim_type,
        "scope": scope,
        "volatility": volatility,
        "source_agent": source_agent,
        "visibility": visibility,
        "holder": holder,
        "confidence": confidence,
        "event_time": event_time,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "intake_batch_id": intake_batch_id,
        "tenant_id": tenant_id,
        **citation_metadata,
    })
    redacted_text, findings = _sanitize_memory_claim_text(text)
    redacted_object, object_findings = _sanitize_optional_memory_claim_text(object_value)
    redacted_subject, subject_findings = _sanitize_optional_memory_claim_text(subject)
    redacted_predicate, predicate_findings = _sanitize_optional_memory_claim_text(predicate)
    sanitized_citations, citation_findings = _sanitize_claim_citations(citations)
    findings.extend(object_findings + subject_findings + predicate_findings + citation_findings)
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
