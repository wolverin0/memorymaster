"""Read-only retained-extraction inventory; contains no recovery/apply action."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from memorymaster.stores._storage_shared import connect_ro


def inventory(path: str | Path, *, scope: str, now: datetime | None = None) -> dict:
    current = now or datetime.now(timezone.utc)
    with connect_ro(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(dream_captures)")}
        retained = " AND c.resume_eligible=0" if "resume_eligible" in columns else ""
        rows = conn.execute(
            "SELECT c.id,c.captured_at,c.provider,c.content_hash,c.extraction_json,"
            "c.decisions_json,r.dry_run FROM dream_captures c "
            "LEFT JOIN dream_runs r ON r.run_id=c.run_id "
            "WHERE c.state='extracted' AND c.scope=?" + retained + " ORDER BY c.id", (scope,),
        ).fetchall()
    items = [_item(dict(row), current) for row in rows]
    canonical = json.dumps(items, sort_keys=True, separators=(",", ":"))
    return {"version": 1, "count": len(items), "items": items,
            "fingerprint": hashlib.sha256(canonical.encode()).hexdigest(), "read_only": True}


def _item(row: dict, now: datetime) -> dict:
    extraction = json.loads(row["extraction_json"] or "[]")
    payload = json.dumps(extraction, sort_keys=True, separators=(",", ":"))
    captured = datetime.fromisoformat(row["captured_at"])
    return {"id": row["id"], "captured_at": row["captured_at"],
            "age_seconds": max(0, int((now - captured).total_seconds())),
            "provider": row["provider"], "content_fingerprint": row["content_hash"],
            "origin": "unknown" if row["dry_run"] is None else "dry_run" if row["dry_run"] else "application",
            "candidates": len(extraction), "decisions": len(json.loads(row["decisions_json"] or "[]")),
            "extraction_fingerprint": hashlib.sha256(payload.encode()).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--scope", required=True)
    args = parser.parse_args()
    print(json.dumps(inventory(args.ledger, scope=args.scope), indent=2))


if __name__ == "__main__":
    main()
