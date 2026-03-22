"""QMD ↔ memorymaster claim mapping.

Bridges OpenClaw's QMD (Quick Memory Database) format with memorymaster claims.
QMD stores memories as simple text entries with type/tier metadata.
This module imports/exports between the two formats.

QMD format (from OpenClaw):
    {type: "fact|event|procedure|constraint|commitment|preference",
     tier: "core|working|peripheral",
     text: "memory content"}

memorymaster format:
    Claim with claim_type, scope, confidence, citations, etc.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from memorymaster.models import CitationInput

logger = logging.getLogger(__name__)

# QMD type → memorymaster claim_type
TYPE_MAP = {
    "fact": "fact",
    "event": "event",
    "procedure": "procedure",
    "constraint": "constraint",
    "commitment": "commitment",
    "preference": "preference",
}

# QMD tier → memorymaster scope
TIER_TO_SCOPE = {
    "core": "global",
    "working": "project",
    "peripheral": "project",
}

# memorymaster scope → QMD tier
SCOPE_TO_TIER = {
    "global": "core",
    "project": "working",
}


def qmd_to_claims(qmd_entries: list[dict[str, Any]], source: str = "qmd-import") -> list[dict[str, Any]]:
    """Convert QMD entries to memorymaster ingest parameters.

    Returns list of dicts ready to pass to service.ingest().
    """
    results = []
    for entry in qmd_entries:
        text = entry.get("text", "").strip()
        if not text:
            continue
        qmd_type = entry.get("type", "fact")
        qmd_tier = entry.get("tier", "working")

        results.append({
            "text": text,
            "citations": [CitationInput(source=source)],
            "claim_type": TYPE_MAP.get(qmd_type, "fact"),
            "scope": TIER_TO_SCOPE.get(qmd_tier, "project"),
            "confidence": 0.7 if qmd_tier == "core" else 0.5,
            "idempotency_key": f"qmd-{hash(text)}",
        })
    return results


def claims_to_qmd(claims: list) -> list[dict[str, Any]]:
    """Convert memorymaster claims to QMD format for export to OpenClaw."""
    results = []
    for claim in claims:
        scope = getattr(claim, "scope", "project")
        base_scope = scope.split(":")[0] if ":" in scope else scope

        results.append({
            "type": getattr(claim, "claim_type", "fact") or "fact",
            "tier": SCOPE_TO_TIER.get(base_scope, "working"),
            "text": claim.text,
        })
    return results


def import_qmd_file(service, file_path: str, source: str = "qmd-import") -> dict[str, int]:
    """Import a QMD JSONL file into memorymaster."""
    from pathlib import Path

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"QMD file not found: {file_path}")

    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    claim_params = qmd_to_claims(entries, source=source)
    ingested = 0
    errors = 0

    for params in claim_params:
        try:
            service.ingest(**params)
            ingested += 1
        except Exception as exc:
            errors += 1
            logger.warning("QMD import failed for entry: %s", exc)

    return {"total": len(entries), "ingested": ingested, "errors": errors}


def export_qmd_file(service, file_path: str, status: str = "confirmed") -> dict[str, int]:
    """Export memorymaster claims to QMD JSONL format."""
    claims = service.list_claims(status=status, limit=50_000)
    qmd_entries = claims_to_qmd(claims)

    with open(file_path, "w", encoding="utf-8") as f:
        for entry in qmd_entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return {"exported": len(qmd_entries), "file": file_path}
