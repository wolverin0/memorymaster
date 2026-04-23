"""Report drift between config_templates/hooks and installed ~/.claude/hooks.

Wave 2-F audit (#130) found the installed ``memorymaster-auto-ingest.py``
had diverged into a block-based auto-save rewrite that the template knew
nothing about. Running ``setup_hooks`` would have silently overwritten the
live version with the old template.

This script makes the drift visible so we catch future regressions. It:

1. Lists every file that exists in BOTH dirs with different content.
2. Flags files that exist in only one side.
3. For each drifted file, prints a short diff-stat so a human can decide.

Usage::

    python scripts/check_hook_template_drift.py

Exits 0 if no drift, 1 if drift found. Intended to be wired into CI or
run as a pre-flight check before ``memorymaster-setup-hooks`` runs.

Placeholder handling: the template uses ``__MEMORYMASTER_PROJECT_ROOT__``
as a sentinel that setup_hooks substitutes with the real root. We
substitute it with the current repo root before diffing so the path-only
difference does not count as drift.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "memorymaster" / "config_templates" / "hooks"
INSTALL_DIR = Path(os.path.expanduser("~")) / ".claude" / "hooks"
PLACEHOLDER = "__MEMORYMASTER_PROJECT_ROOT__"


def _normalize_template(text: str, project_root: str) -> str:
    """Replace template placeholders so content-diff ignores install-time paths.

    The sync script writes templates as ``"__MEMORYMASTER_PROJECT_ROOT__"``
    (string literal). setup_hooks substitutes the placeholder with a raw
    string ``r"<project_root>"``. So for the checker, matching installed
    copies means turning the string literal into the same raw-string form
    with *single* backslashes — never double-escape.
    """
    out = text.replace(f'"{PLACEHOLDER}"', f'r"{project_root}"')
    out = out.replace(PLACEHOLDER, project_root)
    return out


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _diff_stat(a: list[str], b: list[str]) -> tuple[int, int]:
    """(added, removed) line counts via unified diff."""
    added = removed = 0
    for line in difflib.unified_diff(a, b, n=0, lineterm=""):
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project-root", default=str(REPO_ROOT),
                    help=f"Project root used to resolve the template placeholder. Default: {REPO_ROOT}")
    ap.add_argument("--verbose", "-v", action="store_true", help="Print full diffs, not just stats")
    args = ap.parse_args()

    if not TEMPLATE_DIR.is_dir():
        print(f"error: template dir missing: {TEMPLATE_DIR}", file=sys.stderr)
        return 2
    if not INSTALL_DIR.is_dir():
        print(f"note: install dir missing: {INSTALL_DIR} — nothing installed", file=sys.stderr)
        return 0

    tpl_files = {p.name: p for p in TEMPLATE_DIR.iterdir() if p.is_file() and p.suffix == ".py"}
    inst_files = {p.name: p for p in INSTALL_DIR.iterdir() if p.is_file() and p.name.startswith("memorymaster-")}

    drifted: list[tuple[str, int, int]] = []
    template_only = sorted(set(tpl_files) - set(inst_files))
    install_only = sorted(set(inst_files) - set(tpl_files))
    both = sorted(set(tpl_files) & set(inst_files))

    for name in both:
        tpl_text = _normalize_template(_read(tpl_files[name]), args.project_root)
        inst_text = _read(inst_files[name])
        if tpl_text == inst_text:
            continue
        tpl_lines = tpl_text.splitlines(keepends=True)
        inst_lines = inst_text.splitlines(keepends=True)
        added, removed = _diff_stat(tpl_lines, inst_lines)
        drifted.append((name, added, removed))
        if args.verbose:
            print(f"\n=== diff template -> installed: {name} ===")
            sys.stdout.writelines(difflib.unified_diff(
                tpl_lines, inst_lines,
                fromfile=f"template/{name}",
                tofile=f"installed/{name}",
                lineterm="",
            ))

    print(f"Template dir: {TEMPLATE_DIR}")
    print(f"Install dir:  {INSTALL_DIR}")
    print()
    print(f"Files in both: {len(both)}  |  drifted: {len(drifted)}")
    print(f"Template-only: {len(template_only)}  |  Install-only: {len(install_only)}")
    print()

    if drifted:
        print("Drift (template -> installed, line deltas):")
        for name, added, removed in drifted:
            print(f"  {name:40s} +{added:<5d} -{removed:<5d}")
    if install_only:
        print()
        print("Installed hooks with NO template (setup_hooks cannot install on a fresh machine):")
        for name in install_only:
            print(f"  {name}")
    if template_only:
        print()
        print("Template hooks not present in install dir:")
        for name in template_only:
            print(f"  {name}")

    exit_code = 1 if (drifted or template_only or install_only) else 0
    if exit_code == 0:
        print("OK — no drift.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
