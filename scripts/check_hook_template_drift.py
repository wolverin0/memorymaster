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

Placeholder handling: templates carry ``__MEMORYMASTER_*__`` sentinels
(project root, db path, python executable) that the installers replace with
machine-specific values. ``compare_hook`` treats each sentinel as "any text on
that line", so only a real logic difference counts as drift; line endings and
the sha stamp setup_hooks appends to the recall hook are ignored too. The
health probe imports ``compare_hook`` for its ``hooks.template_drift`` check.
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
PLACEHOLDER_RE = re.compile(r"__MEMORYMASTER_[A-Z0-9_]+__")
_STAMP_RE = re.compile(r"^# memorymaster-template-sha256: .*$", re.M)


def _norm(text: str) -> str:
    return _STAMP_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n")).rstrip("\n")


def _template_pattern(template_text: str) -> re.Pattern[str]:
    """The template as a regex: literal text, every placeholder a same-line wildcard."""
    return re.compile("[^\n]*?".join(map(re.escape, PLACEHOLDER_RE.split(template_text))))


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


def compare_hook(template_text: str, installed_text: str) -> tuple[int, int] | None:
    """None when the installed hook is the template (placeholders, line endings aside), else (added, removed)."""
    template, installed = _norm(template_text), _norm(installed_text)
    if _template_pattern(template).fullmatch(installed):
        return None
    installed_lines = installed.split("\n")
    template_lines = []
    for line in template.split("\n"):
        if PLACEHOLDER_RE.search(line):  # a placeholder line the installed copy satisfies is not a delta
            pattern = _template_pattern(line)
            line = next((i for i in installed_lines if pattern.fullmatch(i)), line)
        template_lines.append(line)
    return _diff_stat(template_lines, installed_lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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
        tpl_text, inst_text = _read(tpl_files[name]), _read(inst_files[name])
        delta = compare_hook(tpl_text, inst_text)
        if delta is None:
            continue
        drifted.append((name, *delta))
        if args.verbose:
            tpl_lines = tpl_text.splitlines(keepends=True)
            inst_lines = inst_text.splitlines(keepends=True)
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
