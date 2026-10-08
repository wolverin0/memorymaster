"""Archive tenant-less twins that a replica gets when its origin stamps a tenant.

Measured 2026-10-03 on Hermes: Windows stamped tenant ``personal`` on its old
claims, while Hermes still held them tenant-less. The merge keeps tenant in a
claim's identity on purpose (isolation), so every update Windows sent was
inserted again as ``personal``: 17,187 twins. Hermes runs without a tenant, so
its recall returned both copies.

This step keeps the merge boundary as it is and cleans up after it: for each
(text, scope) with live rows both tenant-less and in ``tenant``, the
tenant-less rows are archived through the lifecycle, pointing at the newest
``tenant`` twin. Archived rows are skipped by the merge, so nothing travels back
to the origin. Run it only on a replica whose origin owns ``tenant``.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from memorymaster.core import lifecycle

REASON = "tenant twin of claim {twin} ({tenant})"


def find_twins(db_path: str | Path, tenant: str) -> list[tuple[int, int]]:
    """``(tenant_less_id, twin_id)`` pairs; the twin is the newest live row in ``tenant``."""
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        untenanted: dict[tuple[str, str], list[int]] = defaultdict(list)
        twins: dict[tuple[str, str], int] = {}
        rows = conn.execute(
            "SELECT id, text, scope, tenant_id FROM claims WHERE status != 'archived' ORDER BY id"
        )
        for claim_id, text, scope, tenant_id in rows:
            key = ((text or "").strip(), scope)
            if tenant_id is None:
                untenanted[key].append(int(claim_id))
            elif tenant_id == tenant:
                twins[key] = int(claim_id)  # ordered by id: the newest wins
    finally:
        conn.close()
    return [(claim_id, twins[key]) for key, ids in untenanted.items() if key in twins for claim_id in ids]


def archive_twins(store, db_path: str | Path, tenant: str, *, apply: bool = False) -> dict[str, object]:
    pairs = find_twins(db_path, tenant)
    result: dict[str, object] = {"tenant": tenant, "twins": len(pairs), "archived": 0, "failed": {}}
    if not apply:
        return result
    failed: Counter[str] = Counter()
    archived = 0
    for claim_id, twin_id in pairs:
        try:
            lifecycle.transition_claim(
                store, claim_id, "archived", reason=REASON.format(twin=twin_id, tenant=tenant),
                event_type="dedup", replaced_by_claim_id=twin_id,
                event_payload={"actor": "tenant-twins"},
            )
            archived += 1
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
            failed[type(exc).__name__] += 1
    result.update(archived=archived, failed=dict(failed))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    from memorymaster.core.service import MemoryService

    service = MemoryService(args.db, workspace_root=Path(args.db).resolve().parent)
    print(json.dumps(archive_twins(service.store, args.db, args.tenant.strip(), apply=args.apply)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
