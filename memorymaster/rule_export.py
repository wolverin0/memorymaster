"""Export mined rule-shaped claims (v3.28).

The rule-miner ingests behavioural rules as ``claim_type='rule'`` claims (see
:mod:`memorymaster.rules`). This module renders those rules for humans/tools in
three formats — ``json``, ``csv``, ``markdown`` — filtered by a minimum
confidence and an optional status, with each rule's ``correction_count``
attached from the ``rule_stats`` tally (the confidence-bootstrap counter).

Used by the ``export-rules`` CLI handler and the ``rules_export`` MCP tool, so
both share one filter/shape and cannot drift. Output is ASCII-safe so a
Windows cp1252 console does not raise ``UnicodeEncodeError`` on rendering.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from typing import Any

from memorymaster.rule_miner import rule_fingerprint
from memorymaster.rules import is_rule, parse_rule

EXPORT_FORMATS = ("json", "csv", "markdown")
_EXPORT_FIELDS = (
    "claim_id",
    "trigger",
    "action",
    "rationale",
    "confidence",
    "correction_count",
    "status",
    "created_at",
)
# Over-fetch factor: rules are a small fraction of claims and list_claims has no
# claim_type filter, so we pull a wide page and filter to rule-typed in Python.
_OVERFETCH = 20


def _correction_counts(db_path: str, fingerprints: set[str]) -> dict[str, int]:
    """Map ``rule_fingerprint -> correction_count`` from ``rule_stats``.

    Returns an empty map (every rule reports count 1) when the table is absent —
    e.g. a DB that never ran the miner with bootstrap enabled.
    """
    if not db_path or "://" in db_path or not fingerprints:
        return {}
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT rule_fingerprint, correction_count FROM rule_stats"
            ).fetchall()
        except sqlite3.Error:
            return {}
        return {
            row["rule_fingerprint"]: int(row["correction_count"])
            for row in rows
            if row["rule_fingerprint"] in fingerprints
        }
    finally:
        conn.close()


def collect_rules(
    service: Any,
    *,
    min_confidence: float = 0.0,
    status: str | None = None,
    limit: int = 500,
    allow_sensitive: bool = False,
) -> list[dict[str, Any]]:
    """Return rule rows for export, filtered by confidence + status.

    Enumerates rule-typed claims (not query-ranked), drops any whose confidence
    is below ``min_confidence``, and attaches ``correction_count`` from
    ``rule_stats``. Sensitive rules are filtered by ``service.list_claims``
    unless ``allow_sensitive`` is granted, so this never leaks secrets at rest.
    """
    claims = service.list_claims(
        status=status,
        limit=max(limit * _OVERFETCH, 100),
        allow_sensitive=allow_sensitive,
    )
    rules = [c for c in claims if is_rule(c)]

    fingerprints: dict[int, str] = {}
    parsed_rules: list[tuple[Any, dict[str, Any]]] = []
    for claim in rules:
        parsed = parse_rule(claim)
        if parsed is None:
            continue
        parsed_rules.append((claim, parsed))
        fingerprints[claim.id] = rule_fingerprint(parsed["trigger"], parsed["action"])

    db_path = str(getattr(getattr(service, "store", None), "db_path", "") or "")
    counts = _correction_counts(db_path, set(fingerprints.values()))

    out: list[dict[str, Any]] = []
    for claim, parsed in parsed_rules:
        confidence = float(claim.confidence or 0.0)
        if confidence < min_confidence:
            continue
        out.append(
            {
                "claim_id": claim.id,
                "trigger": parsed["trigger"],
                "action": parsed["action"],
                "rationale": parsed["rationale"],
                "confidence": round(confidence, 4),
                "correction_count": counts.get(fingerprints[claim.id], 1),
                "status": claim.status,
                "created_at": claim.created_at,
            }
        )
        if len(out) >= limit:
            break
    out.sort(key=lambda r: (-r["confidence"], -r["correction_count"]))
    return out


def render_rules(rows: list[dict[str, Any]], fmt: str) -> str:
    """Render rule rows as ``json``, ``csv``, or ``markdown`` (ASCII-safe)."""
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"format must be one of {EXPORT_FORMATS}, got {fmt!r}")
    if fmt == "json":
        return json.dumps(rows, indent=2, ensure_ascii=True)
    if fmt == "csv":
        return _render_csv(rows)
    return _render_markdown(rows)


def _render_csv(rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(_EXPORT_FIELDS), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().rstrip("\r\n")


def _md_cell(value: Any) -> str:
    """ASCII-safe markdown table cell: escape pipes, flatten newlines."""
    text = str(value if value is not None else "")
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _render_markdown(rows: list[dict[str, Any]]) -> str:
    header = "| " + " | ".join(_EXPORT_FIELDS) + " |"
    sep = "| " + " | ".join("---" for _ in _EXPORT_FIELDS) + " |"
    lines = [header, sep]
    for row in rows:
        lines.append("| " + " | ".join(_md_cell(row.get(f)) for f in _EXPORT_FIELDS) + " |")
    if not rows:
        lines.append("| " + " | ".join("" for _ in _EXPORT_FIELDS) + " |")
    return "\n".join(lines)
