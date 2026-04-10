from __future__ import annotations

import base64
import json
import os
import re
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
    # GitHub — all token prefixes: ghp (personal), gho (oauth), ghu (user),
    # ghs (server-to-server), ghr (refresh), github_pat_ (fine-grained).
    # Both patterns report as "github_token" so downstream callers get a
    # single canonical finding label.
    ("github_token", re.compile(r"\b(ghp_|gho_|ghu_|ghs_|ghr_)[A-Za-z0-9]{36}\b")),
    ("github_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    # Slack
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    # Telegram bot tokens (<numeric_id>:<token>)
    ("telegram_bot_token", re.compile(r"\b\d{8,}:[A-Za-z0-9_\-]{30,}\b")),
    # Bearer / JWT / private key blocks
    ("jwt_token", re.compile(r"\beyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*\b")),
    ("bearer_token", re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{8,}")),
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


def _redact(text: str) -> tuple[str, list[str]]:
    findings: list[str] = []
    out = text
    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(out):
            findings.append(name)
            out = pattern.sub(f"[REDACTED:{name}]", out)
    return out, findings


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
) -> SanitizedClaimInput:
    redacted_text, findings = _redact(text)
    redacted_object = object_value
    object_findings: list[str] = []
    if object_value:
        redacted_object, object_findings = _redact(object_value)
        findings.extend(object_findings)

    sanitized_citations: list[CitationInput] = []
    citation_findings: list[str] = []
    for cite in citations:
        excerpt = cite.excerpt
        if excerpt:
            excerpt, c_findings = _redact(excerpt)
            citation_findings.extend(c_findings)
        sanitized_citations.append(CitationInput(source=cite.source, locator=cite.locator, excerpt=excerpt))
    findings.extend(citation_findings)

    dedup_findings = sorted(set(findings))
    is_sensitive = len(dedup_findings) > 0
    encrypted_payload = _encrypt_payload(
        {
            "text": text,
            "object_value": object_value,
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
    )


def is_sensitive_claim(claim: Claim) -> bool:
    combined = " ".join(
        part for part in [claim.text, claim.object_value or "", claim.subject or "", claim.predicate or ""] if part
    )
    if "[REDACTED:" in combined:
        return True
    _, findings = _redact(combined)
    return len(findings) > 0
