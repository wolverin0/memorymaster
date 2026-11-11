#!/usr/bin/env python
"""Read-only sweep (D-13): confirmed claims filed under a scope that is really a worktree or copy of a project.

Lists ``claim_id scope target_scope`` for every confirmed claim whose ``project:<slug>`` scope resolves to a
different project scope under the CURRENT resolver (``core.scope_utils``): the operator alias file and the
canonical slug first, then - for each ``--root`` that holds a folder named ``<slug>`` (or ``_wt/<slug>``) - the
git topology of that folder, which is how a linked worktree maps to its owner. Ids and scopes only, never claim
text. Applies nothing: the database is opened ``mode=ro``. To move the listed claims, call
``memorymaster.govern.rescope.rescope_claim(store, claim_id, target_scope, reason=...)`` through the service.

    python scripts/find_worktree_scopes.py --db memorymaster.db [--root DIR ...] [--counts]
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memorymaster.core.scope_utils import (  # noqa: E402
    _alias_slug,
    SCOPE_REGISTRY_ENV,
    _scope_aliases,
    canonicalize_slug,
    scope_from_cwd,
)

PREFIX = "project:"


def target_scope(scope: str, roots: list[Path]) -> str | None:
    """The scope this one resolves to now, or None when it already is canonical (or is not a project scope)."""
    if not scope.startswith(PREFIX):
        return None
    slug = scope[len(PREFIX):]
    aliases = _scope_aliases()
    canonical = canonicalize_slug(slug)
    resolved = aliases.get(_alias_slug(slug), aliases.get(canonical))
    if resolved is None:
        for root in roots:
            for folder in (root / slug, root / "_wt" / slug):
                if folder.is_dir():
                    resolved = scope_from_cwd(folder)
                    break
            if resolved is not None:
                break
    return resolved if resolved not in (None, "global", scope) else None


@contextlib.contextmanager
def _scratch_registry():
    """scope_from_cwd registers every git root it meets; point that registry at a throwaway file so a sweep writes nothing."""
    with tempfile.TemporaryDirectory(prefix="mm-scope-sweep-") as tmp:
        saved = os.environ.get(SCOPE_REGISTRY_ENV)
        os.environ[SCOPE_REGISTRY_ENV] = str(Path(tmp) / "scope-registry.json")
        try:
            yield
        finally:
            if saved is None:
                os.environ.pop(SCOPE_REGISTRY_ENV, None)
            else:
                os.environ[SCOPE_REGISTRY_ENV] = saved


def sweep(db: str, roots: list[Path]) -> dict:
    con = sqlite3.connect(f"file:{Path(db).resolve().as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        cache: dict[str, str | None] = {}
        moves: list[tuple[int, str, str]] = []
        scanned = 0
        for claim_id, scope in con.execute("SELECT id, scope FROM claims WHERE status = 'confirmed' ORDER BY id"):
            scanned += 1
            if scope not in cache:
                cache[scope] = target_scope(scope, roots)
            if cache[scope] is not None:
                moves.append((claim_id, scope, cache[scope]))
        return {"scanned": scanned, "moves": moves}
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default="memorymaster.db")
    parser.add_argument("--root", action="append", default=[], type=Path,
                        help="folder holding the project and worktree checkouts (repeatable)")
    parser.add_argument("--counts", action="store_true", help="print only the counts per scope")
    args = parser.parse_args(argv)
    if not Path(args.db).is_file():
        print(f"error: database not found: {args.db}", file=sys.stderr)
        return 2
    with _scratch_registry():
        result = sweep(args.db, args.root)
    if args.counts:
        per_scope: dict[tuple[str, str], int] = {}
        for _, scope, target in result["moves"]:
            per_scope[(scope, target)] = per_scope.get((scope, target), 0) + 1
        for (scope, target), count in sorted(per_scope.items()):
            print(f"{count}\t{scope}\t{target}")
    else:
        for claim_id, scope, target in result["moves"]:
            print(f"{claim_id}\t{scope}\t{target}")
    print(f"confirmed_scanned={result['scanned']} claims_to_move={len(result['moves'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
