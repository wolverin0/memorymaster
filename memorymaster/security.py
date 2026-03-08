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
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt_token", re.compile(r"\beyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*\b")),
    ("github_token", re.compile(r"\b(ghp_[A-Za-z0-9]{36}|gho_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,})\b")),
    ("bearer_token", re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{8,}")),
    ("password_assignment", re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*([^\s,;]+)")),
    ("token_assignment", re.compile(r"(?i)\b(token|api[_-]?key|secret)\s*[:=]\s*([^\s,;]+)")),
    # Hex tokens (API keys, session tokens) - standalone 40+ hex chars in backticks or after labels
    ("hex_token", re.compile(r"`([0-9a-f]{40,})`")),
    # Hex tokens near keyword context (keyword within 80 chars before hex)
    ("hex_token_ctx", re.compile(r"(?i)(?:token|key|secret|credential).{0,80}?([0-9a-f]{40,})")),
    # Markdown credential patterns: **Pass**: value, **Password**: value, etc.
    ("markdown_credential", re.compile(r"(?i)\*\*(?:pass(?:word)?|pwd|secret|token|key|credential)s?\*\*\s*[:=]\s*`?([^\s`,;\n]+)")),
    # Inline backtick credentials after label: `TOKEN`: `value` or TOKEN: `value`
    ("inline_credential", re.compile(r"(?i)(?:_?(?:api_?)?(?:token|key|secret|password|credential)s?_?)`?\s*[:=]\s*`([^`]+)`")),
    # SSH/connection strings with embedded passwords
    ("connection_password", re.compile(r"(?i)(?:ssh|ftp|mysql|postgres|redis|mongo).*(?:password|pass|pwd)\s*[:=]\s*([^\s,;]+)")),
]

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
