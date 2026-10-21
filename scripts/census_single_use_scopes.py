"""Report claims in suspicious single-use scopes without changing SQLite.

Run explicitly with ``python -m scripts.census_single_use_scopes --dry-run
--db <sqlite-path>``. JSON lines contain only claim IDs and their current scope;
matches are review candidates, not instructions to rescope or merge claims.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys


_SINGLE_USE_SCOPE = re.compile(
    r"project:(?:"
    r"t-?[0-9]+|t-card|"
    r"[a-z0-9_-]+-pr[0-9]+|"
    r"(?:_?wt|_?worktrees?)(?:[-_][a-z0-9_-]+)?|"
    r"[a-z0-9_-]+[-_](?:wt|worktrees?)(?:[-_][a-z0-9_-]+)?"
    r")",
    re.IGNORECASE,
)


def is_single_use_scope(scope: str) -> bool:
    """Recognize explicit task, PR and worktree markers; do not infer aliases."""
    return _SINGLE_USE_SCOPE.fullmatch(scope) is not None


def affected_claims(db_path: Path) -> list[tuple[int, str]]:
    """Read only IDs/scopes from an existing, explicitly supplied database."""
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute("SELECT id, scope FROM claims ORDER BY id")
        affected = []
        for claim_id, scope in rows:
            if not isinstance(claim_id, int) or not isinstance(scope, str):
                raise ValueError("Unexpected claim ID or scope type")
            if is_single_use_scope(scope):
                affected.append((claim_id, scope))
        return affected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", required=True, help="Required read-only census acknowledgement")
    parser.add_argument("--db", type=Path, required=True, help="Explicit SQLite database path; no default database is opened")
    args = parser.parse_args(argv)
    try:
        rows = affected_claims(args.db)
    except (OSError, ValueError, sqlite3.Error):
        print("Census failed: cannot read claim IDs/scopes from the supplied SQLite database.", file=sys.stderr)
        return 1
    for claim_id, scope in rows:
        print(json.dumps({"claim_id": claim_id, "current_scope": scope}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
