#!/usr/bin/env python3
"""Fail when a working branch has fallen too far behind its base.

Editing on a stale checkout is silent and expensive: the files look normal, the
tests pass, and the divergence only surfaces later — as a merge conflict, or as
a patch that no longer applies to code that moved underneath it. On 2026-08-18
work was twice started on a branch 159 commits behind main, and both files
touched had since changed on main, one of them by 117 lines.

Nothing reports this on its own. ``git status`` is silent about a branch that is
merely old, and a stale branch's tests pass exactly as well as a fresh one's.

Run with no arguments before starting work:

    python scripts/check_branch_freshness.py

CI runs it on every pull request, where a head branch far behind its base means
the diff under review is not the diff that will land.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

DEFAULT_MAX_BEHIND = 40


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def commits_behind(base: str) -> int:
    """How many commits exist on ``base`` that HEAD does not have."""
    return int(_git("rev-list", "--count", f"HEAD..{base}"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base",
        default="origin/main",
        help="Ref to compare against (default: origin/main).",
    )
    parser.add_argument(
        "--max-behind",
        type=int,
        default=DEFAULT_MAX_BEHIND,
        help=f"Fail above this many commits behind (default: {DEFAULT_MAX_BEHIND}).",
    )
    args = parser.parse_args(argv)

    try:
        behind = commits_behind(args.base)
    except subprocess.CalledProcessError as exc:
        # An unfetched or unknown base is a setup problem, not a stale branch.
        # Say so plainly rather than failing the caller's work over it.
        print(
            f"could not compare against {args.base}: {exc.stderr.strip() or exc}\n"
            f"try: git fetch origin {args.base.split('/')[-1]}",
            file=sys.stderr,
        )
        return 0

    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if behind > args.max_behind:
        print(
            f"'{branch}' is {behind} commits behind {args.base} "
            f"(limit {args.max_behind}).\n"
            f"Editing here builds on code that has since moved. Rebase, or start "
            f"a worktree from {args.base}:\n"
            f"    git worktree add ../work -b <branch> {args.base}",
            file=sys.stderr,
        )
        return 1

    print(f"'{branch}' is {behind} commits behind {args.base} — ok.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
