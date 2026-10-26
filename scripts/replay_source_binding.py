"""Count old/new source completeness for recent applied captures, without writes.

Usage: python scripts/replay_source_binding.py CAPTURE_CONTROL_DB [--days 30]
Counts are per extracted candidate in captures with nonempty decisions, using
captured_at for the lookback window. No source text or decisions are printed.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memorymaster.dreaming.held import candidate_fields  # noqa: E402
from memorymaster.dreaming.models import DreamCandidate  # noqa: E402
from memorymaster.dreaming.source_review import (  # noqa: E402
    MAX_SOURCE_CHARS, SourceCandidate, _hash, _source_kind, bind_source, safe_context,
)


def _bind_source_before(candidate: DreamCandidate, row: dict) -> SourceCandidate:
    """Frozen pre-T-1799 binding logic, including its full-context hash."""
    messages = [{key: message.get(key, "") for key in ("message_id", "role", "text", "timestamp")}
                for message in row.get("messages", [])]
    context = {"capture_scope": row["scope"], "messages": messages}
    source_hash = _hash({"candidate": candidate.to_dict(), "context": context,
                         "provider": row.get("provider"), "session": row.get("session_hash")})
    safe = safe_context(context)
    complete = bool(messages)
    if len(json.dumps(safe, ensure_ascii=False)) > MAX_SOURCE_CHARS:
        safe["messages"] = []
        complete = False
    evidence = next((m for m in messages if m["message_id"] == candidate.evidence_message_id), None)
    exact = bool(evidence and candidate.evidence_quote and candidate.evidence_quote in evidence["text"])
    return SourceCandidate(**candidate.to_dict(), source_context={
        **safe, "source_hash": source_hash, "complete": complete, "exact_quote": exact,
        "candidate_safe": safe_context(candidate.to_dict()) == candidate.to_dict(),
        "source_kind": _source_kind(evidence),
    })


def replay(database: str | Path, *, days: int = 30, now: datetime | None = None) -> dict[str, int]:
    if days < 1:
        raise ValueError("days must be positive")
    now = now or datetime.now(timezone.utc)
    counts = dict.fromkeys(("total", "complete_before", "complete_after", "flipped_to_complete"), 0)
    # Do not construct DreamLedger: its initialization can migrate/write the DB.
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        rows = conn.execute(
            "SELECT scope, provider, session_hash, messages_json, extraction_json, decisions_json "
            "FROM dream_captures WHERE state='applied' AND decisions_json IS NOT NULL "
            "AND julianday(captured_at) >= julianday(?) AND julianday(captured_at) <= julianday(?) "
            "ORDER BY id", ((now - timedelta(days=days)).isoformat(), now.isoformat()),
        )
        for stored in rows:
            if not json.loads(stored["decisions_json"]):
                continue
            row = dict(stored)
            row["messages"] = json.loads(row["messages_json"])
            for payload in json.loads(row["extraction_json"] or "[]"):
                candidate = DreamCandidate(**candidate_fields(payload))
                before = _bind_source_before(candidate, row).source_context["complete"]
                after = bind_source(candidate, row).source_context["complete"]
                counts["total"] += 1
                counts["complete_before"] += before
                counts["complete_after"] += after
                counts["flipped_to_complete"] += after and not before
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="capture-control SQLite database")
    parser.add_argument("--days", type=int, default=30, help="lookback by captured_at (default: 30)")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be positive")
    print(json.dumps(replay(args.database, days=args.days)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
