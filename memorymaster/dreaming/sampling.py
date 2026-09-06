"""Content-free source census and bounded stratified sample; no provider or DB writes."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3


_QUERY = """
SELECT c.id, c.content_hash, c.session_hash, c.turn_count,
       c.extraction_json,
       (SELECT count(*) FROM dream_applications a WHERE a.capture_id=c.id) AS actions
FROM dream_captures c
WHERE julianday(c.captured_at)>=julianday(?) AND julianday(c.captured_at)<julianday(?)
ORDER BY c.id
"""


def _date(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("window dates must include timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _entry(row: sqlite3.Row) -> dict:
    raw = row["extraction_json"]
    count = None
    if raw is None:
        stratum = "unprocessed"
    else:
        try:
            extracted = json.loads(raw)
            if not isinstance(extracted, list):
                raise ValueError("not an extraction list")
            count = len(extracted)
            stratum = ("zero_candidates" if count == 0 else
                       "candidates_with_actions" if row["actions"] else "candidates_no_actions")
        except (ValueError, TypeError):
            stratum = "malformed"
    return {
        "capture_id": row["id"], "source_fingerprint": _hash(row["content_hash"]),
        "session_fingerprint": _hash(row["session_hash"]),
        "extraction_fingerprint": _hash(raw), "turn_count": row["turn_count"],
        "candidate_count": count, "action_count": row["actions"], "stratum": stratum,
    }


def sample_sources(ledger: str | Path, *, since: str, until: str,
                   per_stratum: int = 5) -> dict:
    """Sample all source strata, not just successful additions; values stay local."""
    since, until = _date(since), _date(until)
    if since >= until or type(per_stratum) is not int or not 1 <= per_stratum <= 100:
        raise ValueError("invalid window or per-stratum limit")
    connection = sqlite3.connect(Path(ledger).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    counts, selected = Counter(), {}
    fingerprint = hashlib.sha256()
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        for row in connection.execute(_QUERY, (since, until)):
            entry = _entry(row)
            fingerprint.update(json.dumps(entry, sort_keys=True).encode())
            stratum = entry["stratum"]
            counts[stratum] += 1
            selected[stratum] = sorted(
                [*selected.get(stratum, []), entry], key=lambda item: _hash(item),
            )[:per_stratum]
    finally:
        connection.close()
    return {
        "schema": "memorymaster.dreaming.source-sample.v1",
        "since": since, "until": until, "per_stratum": per_stratum,
        "population": sum(counts.values()), "counts": dict(sorted(counts.items())),
        "population_fingerprint": fingerprint.hexdigest(),
        "sample": [entry for key in sorted(selected) for entry in selected[key]],
        "semantic_precision": None, "useful_recall": None,
        "label_requirement": "Source-level expected-fact labels are still required; counts are not quality.",
    }
