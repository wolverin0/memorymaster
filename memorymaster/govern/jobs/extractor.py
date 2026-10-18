from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

_SPACE_RE = re.compile(r"\s+")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s]+")
_UNIX_PATH_RE = re.compile(r"(?:/[^/\s]+)+")


# Documentation hosts whose URLs are references, not a fact about a service of ours
# (T-1591). Deliberately minimal: extend only with a measured offender.
GENERIC_DOCS_DOMAINS = ("code.claude.com",)


def _is_loopback_host(host: str | None) -> bool:
    host = (host or "").strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_generic_docs_host(host: str | None) -> bool:
    host = (host or "").lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in GENERIC_DOCS_DOMAINS)


def _url_host(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def _first_infra_ip(text: str) -> str | None:
    """First IPv4 in the text that is not loopback (127.0.0.0/8 says nothing about a server)."""
    for match in _IPV4_RE.finditer(text):
        if not _is_loopback_host(match.group(0)):
            return match.group(0)
    return None


def _first_service_url(text: str) -> str | None:
    """First URL that is neither loopback nor a generic docs reference."""
    for match in _URL_RE.finditer(text):
        host = _url_host(match.group(0))
        if not _is_loopback_host(host) and not _is_generic_docs_host(host):
            return match.group(0)
    return None


def normalize_claim_text(text: str) -> str:
    return _SPACE_RE.sub(" ", text.strip().lower())


def infer_structure(text: str) -> tuple[str | None, str | None, str | None, str | None]:
    # Loopback addresses and generic docs URLs are prose, not infra facts: stamping
    # them as server/ip_address or service/url put a bogus triple on long claims
    # that merely mention them (T-1591).
    ip = _first_infra_ip(text)
    if ip:
        return ("infra_fact", "server", "ip_address", ip)

    url = _first_service_url(text)
    if url:
        return ("infra_fact", "service", "url", url)

    # URLs skipped above must not be re-read as filesystem paths ("//host/page").
    prose = _URL_RE.sub(" ", text)
    path_match = _WIN_PATH_RE.search(prose) or _UNIX_PATH_RE.search(prose)
    if path_match:
        # Use None instead of generic "workspace"/"path" to avoid mass conflicts
        # Claims with file paths don't need tuple-based conflict detection
        return ("filesystem_fact", None, None, None)

    if "credential" in text.lower() or "secret" in text.lower():
        return ("security_fact", "auth", "location_hint", text.strip())

    return (None, None, None, None)


def run(store, limit: int = 200) -> dict[str, int]:
    claims = store.find_by_status("candidate", limit=limit)
    normalized = 0
    structured = 0

    # Batch normalize texts
    normalized_texts = {}
    for claim in claims:
        clean = normalize_claim_text(claim.text)
        normalized_texts[claim.id] = clean
        normalized += 1

    if normalized_texts:
        if hasattr(store, "set_normalized_texts_batch"):
            store.set_normalized_texts_batch(normalized_texts)
        else:
            # Fallback for stores without batch method
            for claim_id, clean in normalized_texts.items():
                store.set_normalized_text(claim_id, clean)

    # Process structure inference
    for claim in claims:
        claim_type, subject, predicate, object_value = infer_structure(claim.text)
        if any([claim_type, subject, predicate, object_value]):
            store.update_claim_structure(
                claim.id,
                claim_type=claim_type,
                subject=subject,
                predicate=predicate,
                object_value=object_value,
            )
            structured += 1
            store.record_event(
                claim_id=claim.id,
                event_type="extractor",
                details="structure_inferred",
                payload={
                    "claim_type": claim_type,
                    "subject": subject,
                    "predicate": predicate,
                    "object_value": object_value,
                },
            )

    return {"processed": len(claims), "normalized": normalized, "structured": structured}
