from __future__ import annotations

import re

_SPACE_RE = re.compile(r"\s+")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s]+")
_UNIX_PATH_RE = re.compile(r"(?:/[^/\s]+)+")


def normalize_claim_text(text: str) -> str:
    return _SPACE_RE.sub(" ", text.strip().lower())


def infer_structure(text: str) -> tuple[str | None, str | None, str | None, str | None]:
    ip_match = _IPV4_RE.search(text)
    if ip_match:
        return ("infra_fact", "server", "ip_address", ip_match.group(0))

    url_match = _URL_RE.search(text)
    if url_match:
        return ("infra_fact", "service", "url", url_match.group(0))

    path_match = _WIN_PATH_RE.search(text) or _UNIX_PATH_RE.search(text)
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
