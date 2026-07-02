"""Generate docs/env-reference.md — the complete MEMORYMASTER_* env-var inventory.

Fresh-eyes audit (2026-07-01) found ~70 of 133 env vars documented nowhere,
including security-relevant ones. Hand-typed inventories rot; this one is
grep-derived from the code, so re-running it after any change keeps the doc
honest. Usage:

    python scripts/gen_env_reference.py
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "memorymaster"
OUT = REPO / "docs" / "env-reference.md"
VAR_RE = re.compile(r"\bMEMORYMASTER_[A-Z0-9_]+\b")


def main() -> int:
    var_files: dict[str, set[str]] = defaultdict(set)
    for path in sorted(PKG.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(REPO).as_posix()
        for match in VAR_RE.findall(text):
            var_files[match].add(rel)

    lines = [
        "# Environment variable reference",
        "",
        "Complete inventory of `MEMORYMASTER_*` variables referenced in the package,",
        "**generated** by `scripts/gen_env_reference.py` — do not hand-edit; re-run the",
        "script after adding or removing a variable. For what each does, follow the",
        "listed source files (most are read next to a docstring or comment).",
        "",
        f"Total: {len(var_files)} variables.",
        "",
        "| Variable | Referenced in |",
        "|---|---|",
    ]
    for var in sorted(var_files):
        files = ", ".join(f"`{f}`" for f in sorted(var_files[var])[:4])
        extra = len(var_files[var]) - 4
        if extra > 0:
            files += f" (+{extra} more)"
        lines.append(f"| `{var}` | {files} |")
    lines.append("")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT} ({len(var_files)} vars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
