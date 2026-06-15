"""Merge fragmented scope variants in the claims DB into their canonical form.

Follow-up to the 2026-04-22 scope-fragmentation audit. Uses the SAME
``_canonicalize_slug`` helper as ``memorymaster.surfaces.mcp_server`` so code and
migration agree by construction.

Usage::

    python scripts/merge_scope_variants.py --db memorymaster.db --dry-run
    python scripts/merge_scope_variants.py --db memorymaster.db --apply

Default is ``--dry-run`` — it prints a merge plan and exits 0 without writing.
``--apply`` wraps all UPDATEs in a single transaction; on any error the entire
migration rolls back.

Hard rule: scopes matching the ``--except`` glob are ALWAYS preserved as-is.
The default pattern ``project:pauol:%`` protects the user-namespace scope.
This script does NOT auto-run anywhere; it must be invoked by a human.
"""

from __future__ import annotations

import argparse
import fnmatch
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Allow running as ``python scripts/merge_scope_variants.py`` from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorymaster.surfaces.mcp_server import _canonicalize_slug  # noqa: E402


def _canonicalize_scope(scope: str) -> str:
    """Map a stored scope string to its canonical form.

    - ``project:<slug>`` and ``project:<slug>:<hash>`` both fold to
      ``project:<canonical_slug>`` (the hash is preserved ONLY if we were
      running the mcp_server with ``MEMORYMASTER_SCOPE_DISAMBIGUATE=1``, which
      the migration cannot tell retroactively — so we strip it here, matching
      the default canonicalizer).
    - Bare ``project`` → ``user`` (matches new empty-workspace behaviour).
    - Non-project scopes (``global``, ``user``, ``team:foo``) are passed
      through unchanged.
    """
    s = (scope or "").strip()
    if not s:
        return s
    if s == "project":
        return "user"
    if not s.startswith("project:"):
        return s
    parts = s.split(":", 2)
    # parts = ["project", "<slug-or-slug-with-suffix>", maybe "<hash>"]
    slug = parts[1] if len(parts) > 1 else ""
    canonical = _canonicalize_slug(slug)
    return f"project:{canonical}"


def _matches_any(scope: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(scope, pat) for pat in patterns)


def _plan(conn: sqlite3.Connection, except_patterns: list[str]) -> list[tuple[str, str, int]]:
    """Return a list of (old_scope, new_scope, claim_count) merge rows.

    Only includes scopes where old != new AND old is not excepted.
    """
    rows = conn.execute("SELECT scope, COUNT(*) FROM claims GROUP BY scope").fetchall()
    plan: dict[tuple[str, str], int] = defaultdict(int)
    for scope, count in rows:
        if scope is None:
            continue
        if _matches_any(scope, except_patterns):
            continue
        canonical = _canonicalize_scope(scope)
        if canonical != scope:
            plan[(scope, canonical)] += count
    return sorted(
        [(old, new, n) for (old, new), n in plan.items()],
        key=lambda r: (-r[2], r[0]),
    )


def _archive_confirmed_collisions(conn: sqlite3.Connection, old: str, new: str) -> int:
    """Archive older confirmed claims that would collide in the target scope.

    The DB has a partial UNIQUE index ``idx_claims_confirmed_tuple_unique`` on
    ``(subject, predicate, scope) WHERE status = 'confirmed'`` plus trigger
    guards. Re-pointing the scope of a confirmed claim into a scope that
    already holds a confirmed claim with the same ``(subject, predicate)``
    would violate that constraint.

    Resolution policy: keep the more recent of the two (by ``updated_at``);
    archive the older one in-place. Both rows survive, history is preserved,
    and the migration can proceed.
    """
    now = datetime.now(timezone.utc).isoformat()
    # For every confirmed claim in ``old`` whose (subject, predicate) also has
    # a confirmed twin in ``new``, archive whichever one is older.
    rows = conn.execute(
        """
        SELECT a.id AS a_id, a.updated_at AS a_upd,
               b.id AS b_id, b.updated_at AS b_upd
        FROM claims a
        JOIN claims b
          ON a.subject = b.subject
         AND a.predicate = b.predicate
         AND a.status = 'confirmed'
         AND b.status = 'confirmed'
        WHERE a.scope = ? AND b.scope = ?
          AND a.subject IS NOT NULL AND a.predicate IS NOT NULL
        """,
        (old, new),
    ).fetchall()
    archived = 0
    for a_id, a_upd, b_id, b_upd in rows:
        loser = a_id if (a_upd or "") < (b_upd or "") else b_id
        conn.execute(
            "UPDATE claims SET status='archived', archived_at=?, updated_at=? WHERE id=?",
            (now, now, loser),
        )
        archived += 1
    return archived


def _apply(conn: sqlite3.Connection, plan: list[tuple[str, str, int]]) -> tuple[int, int]:
    """Execute the plan inside a single transaction.

    Returns ``(rows_updated, collisions_archived)``.
    """
    total_updated = 0
    total_archived = 0
    try:
        conn.execute("BEGIN")
        for old, new, _count in plan:
            total_archived += _archive_confirmed_collisions(conn, old, new)
            cur = conn.execute(
                "UPDATE claims SET scope = ? WHERE scope = ?",
                (new, old),
            )
            total_updated += cur.rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return total_updated, total_archived


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True, help="Path to memorymaster.db")
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Print plan without writing (default)",
    )
    group.add_argument(
        "--apply",
        dest="apply_changes",
        action="store_true",
        help="Execute the UPDATE statements in a single transaction",
    )
    p.add_argument(
        "--except",
        dest="except_patterns",
        action="append",
        default=None,
        help="Glob pattern(s) of scopes to preserve as-is. "
        "Default: project:pauol:% (user-namespace, preserved by design).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    except_patterns = args.except_patterns or ["project:pauol:*"]

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"error: db not found: {db_path}", file=sys.stderr)
        return 2

    with sqlite3.connect(db_path) as conn:
        plan = _plan(conn, except_patterns)
        print(f"merge plan for {db_path}")
        print(f"  except patterns: {except_patterns}")
        if not plan:
            print("  (nothing to merge — all scopes already canonical)")
            return 0
        print(f"  {len(plan)} merge pairs, {sum(r[2] for r in plan)} claims affected")
        print()
        print(f"  {'OLD SCOPE':<50} -> {'NEW SCOPE':<40} {'CLAIMS':>8}")
        for old, new, count in plan:
            old_disp = old if len(old) <= 48 else old[:45] + "..."
            new_disp = new if len(new) <= 38 else new[:35] + "..."
            print(f"  {old_disp:<50} -> {new_disp:<40} {count:>8}")

        if args.apply_changes:
            print()
            print("applying...")
            updated, archived = _apply(conn, plan)
            print(f"done — {updated} rows updated, {archived} colliding confirmed claims archived")
        else:
            print()
            print("(dry-run — pass --apply to execute)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
