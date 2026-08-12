"""Provider-local compatibility guard for encoded secrets."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

from memorymaster.core.security import sanitize_persisted_text as _upstream_sanitize
from memorymaster.core.security import scan_persisted_value as _upstream_scan


_BASE64_RE = re.compile(
    r"(?<![A-Za-z0-9+/=_-])"
    r"(?:[A-Za-z0-9+/]{20,}={0,2}|[A-Za-z0-9_-]{20,}={0,2})"
    r"(?![A-Za-z0-9+/=_-])"
)
_HEX_ESCAPE_RE = re.compile(r"(?:\\x[0-9A-Fa-f]{2}){4,}")
_MAX_VARIANTS = 32


def sanitize_outbox_text(text: str) -> tuple[str, list[str]]:
    """Sanitize text even when the host MemoryMaster lacks encoded scanning."""
    sanitized, findings = _upstream_sanitize(text)
    encoded_findings = _decoded_secret_findings(text)
    if encoded_findings:
        sanitized = "[REDACTED:encoded_secret]"
    return sanitized, sorted(set(findings + encoded_findings))


def scan_outbox_value(value: object) -> list[str]:
    """Scan an envelope with a bounded encoded-secret compatibility pass."""
    findings = list(_upstream_scan(value))
    findings.extend(_upstream_scan(json.dumps(value, sort_keys=True, default=str)))
    if _has_legacy_findings_metadata(value):
        findings.append("legacy_findings_metadata")
    for text in _iter_strings(value):
        findings.extend(_decoded_secret_findings(text))
    return sorted(set(findings))


def _has_legacy_findings_metadata(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    identity = value.get("identity")
    payload = value.get("payload")
    if not isinstance(identity, Mapping) or not isinstance(payload, Mapping):
        return False
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        return False
    legacy_findings = metadata.get("findings")
    if isinstance(legacy_findings, str):
        legacy_findings = (legacy_findings,)
    if not isinstance(legacy_findings, (list, tuple, set, frozenset)):
        return False
    if not any(
        isinstance(label, str)
        and re.search(r"(?i)token|key|secret|credential", label)
        for label in legacy_findings
    ):
        return False
    hashes = (identity.get("content_hash"), identity.get("session_hash"))
    return all(
        isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
        for digest in hashes
    )


def _decoded_secret_findings(text: str) -> list[str]:
    findings: list[str] = []
    for decoded in _decoded_variants(text):
        _, detected = _upstream_sanitize(decoded)
        findings.extend(detected)
    return sorted(set(findings))


def _decoded_variants(text: str) -> Iterator[str]:
    seen = {text}
    queue = [text]
    while queue and len(seen) <= _MAX_VARIANTS:
        current = queue.pop(0)
        candidates = [_decode_base64(match.group(0)) for match in _BASE64_RE.finditer(current)]
        candidates.extend(_decode_hex(match.group(0)) for match in _HEX_ESCAPE_RE.finditer(current))
        for decoded in candidates:
            if decoded and decoded not in seen:
                seen.add(decoded)
                queue.append(decoded)
                yield decoded


def _decode_base64(candidate: str) -> str | None:
    if len(candidate) % 4 == 1:
        return None
    padded = candidate + ("=" * (-len(candidate) % 4))
    try:
        return _decode_text(base64.b64decode(padded, validate=True))
    except binascii.Error:
        try:
            return _decode_text(base64.urlsafe_b64decode(padded))
        except (binascii.Error, ValueError):
            return None


def _decode_hex(candidate: str) -> str | None:
    raw = bytes(int(pair, 16) for pair in re.findall(r"\\x([0-9A-Fa-f]{2})", candidate))
    return _decode_text(raw)


def _decode_text(raw: bytes) -> str | None:
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not decoded or "\x00" in decoded:
        return None
    printable = sum(char.isprintable() or char in "\r\n\t" for char in decoded)
    return decoded if printable / len(decoded) >= 0.85 else None


def _iter_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str):
                yield key
            yield from _iter_strings(nested)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for nested in value:
            yield from _iter_strings(nested)
