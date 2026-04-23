"""Re-parameterize installed hooks back into the template dir (#130).

Wave 2-F audit found 6 of 7 hook templates had drifted from the installed
copies (plus 2 installed hooks with no template at all — ``dream-sync``
and ``observe``). Re-running ``memorymaster-setup-hooks`` would silently
overwrite live hooks with stale templates.

This script is the one-shot recovery: it copies each installed file from
``~/.claude/hooks/memorymaster-*.py`` into
``memorymaster/config_templates/hooks/`` with the hardcoded project root
replaced back by the ``__MEMORYMASTER_PROJECT_ROOT__`` sentinel so the
setup-hooks installer can re-substitute on any machine.

Usage::

    python scripts/sync_hook_templates.py --dry-run
    python scripts/sync_hook_templates.py --apply

Default is ``--dry-run``. ``--apply`` writes the templates and prints the
list of files touched. Run ``scripts/check_hook_template_drift.py`` after
to verify drift is resolved.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "memorymaster" / "config_templates" / "hooks"
INSTALL_DIR = Path(os.path.expanduser("~")) / ".claude" / "hooks"
PLACEHOLDER = "__MEMORYMASTER_PROJECT_ROOT__"


def _reparameterize(text: str, project_root: str) -> str:
    """Replace every hardcoded project-root reference with the placeholder.

    Handles the forms we've seen in the installed files:
      - ``r"G:\\_OneDrive\\...\\memorymaster"`` (raw-string literal)
      - ``"G:\\\\_OneDrive\\\\...\\\\memorymaster"`` (escaped string literal)
      - ``Path(r"G:\\_OneDrive\\...\\memorymaster")`` (pathlib wrapped)
      - forward-slash variants
    """
    root = project_root
    root_fwd = root.replace("\\", "/")
    # Escaped-string form (double backslashes)
    root_escaped = root.replace("\\", "\\\\")

    out = text
    # Order matters: replace raw-string first so we don't leave stray r prefixes.
    out = out.replace(f'r"{root}"', f'"{PLACEHOLDER}"')
    out = out.replace(f"r'{root}'", f"'{PLACEHOLDER}'")
    out = out.replace(f'"{root_escaped}"', f'"{PLACEHOLDER}"')
    out = out.replace(f"'{root_escaped}'", f"'{PLACEHOLDER}'")
    out = out.replace(f'"{root_fwd}"', f'"{PLACEHOLDER}"')
    out = out.replace(f"'{root_fwd}'", f"'{PLACEHOLDER}'")
    out = out.replace(root, PLACEHOLDER)  # any remaining bare occurrences
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project-root", default=str(REPO_ROOT),
                    help=f"The hardcoded project root to reparameterize. Default: {REPO_ROOT}")
    ap.add_argument("--apply", action="store_true", help="Write templates. Default: dry-run.")
    args = ap.parse_args()

    if not INSTALL_DIR.is_dir():
        print(f"error: install dir missing: {INSTALL_DIR}", file=sys.stderr)
        return 2
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)

    installed = sorted(
        p for p in INSTALL_DIR.iterdir()
        if p.is_file() and p.suffix == ".py" and p.name.startswith("memorymaster-")
    )
    if not installed:
        print("no memorymaster-*.py hooks found in install dir", file=sys.stderr)
        return 1

    actions: list[tuple[str, Path, str, int]] = []
    for p in installed:
        src = p.read_text(encoding="utf-8")
        tpl = _reparameterize(src, args.project_root)
        dest = TEMPLATE_DIR / p.name
        if dest.exists() and dest.read_text(encoding="utf-8") == tpl:
            actions.append(("skip", dest, "identical", len(tpl)))
            continue
        status = "update" if dest.exists() else "create"
        actions.append((status, dest, tpl, len(tpl)))

    print(f"project root being reparameterized: {args.project_root}")
    print(f"template dir: {TEMPLATE_DIR}")
    print()
    for status, dest, _tpl_or_reason, size in actions:
        print(f"  {status:<6} {dest.name:<40s} {size} chars")
    print()
    touched = sum(1 for a in actions if a[0] != "skip")
    print(f"Would touch {touched} file(s).")

    if not args.apply:
        print("DRY-RUN. Re-run with --apply to write.")
        return 0

    for status, dest, tpl, _size in actions:
        if status == "skip":
            continue
        dest.write_text(tpl, encoding="utf-8")
    print(f"Applied. {touched} file(s) written.")
    print("Run `python scripts/check_hook_template_drift.py` to verify.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
