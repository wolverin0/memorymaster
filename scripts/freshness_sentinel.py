#!/usr/bin/env python3
"""Vigila que el checkout canonico no quede detras de origin/main (T-0241).

check_branch_freshness.py ya existe y CI lo corre por PR, pero nada vigilaba el
CANONICO entre PRs: se trabaja en worktrees laterales, el merge ocurre en GitHub
y el checkout principal nunca baja — asi se gestaron 82 commits de divergencia y
la semana de confusion 4.5.0-vs-4.8.4.

Corre sin modelo y sin tokens, desde un schtask cuyo nombre y horario viven en
ops/freshness-sentinel.json (config-by-file). Comportamiento:

- al dia (0 commits detras tras fetch): imprime una linea y sale 0. NO escribe
  ningun aviso — un aviso que sale siempre no es aviso.
- divergente (>=1 commits detras): apendea una linea con el conteo al rollup del
  dia (_intel/rollups/<fecha>.md, creandolo si no existe) y sale 1, con lo que
  el Last Result del schtask queda visible en el census del rollup diario.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(target: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(target), *args], capture_output=True, text=True, check=False
    )


def commits_behind(target: Path, base: str = "origin/main") -> tuple[int, str]:
    """(commits detras de base, rama actual) para el checkout target."""
    fetch = _git(target, "fetch", "--quiet", "origin", "main")
    if fetch.returncode != 0:
        raise RuntimeError(f"git fetch fallo en {target}: {fetch.stderr.strip()[:200]}")
    count = _git(target, "rev-list", "--count", f"HEAD..{base}")
    if count.returncode != 0:
        raise RuntimeError(f"rev-list fallo en {target}: {count.stderr.strip()[:200]}")
    branch = _git(target, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    return int(count.stdout.strip()), branch


def append_notice(rollup_dir: Path, line: str) -> Path:
    rollup_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    path = rollup_dir / f"{today}.md"
    if not path.exists():
        path.write_text(
            f"# Rollup diario del operador — {today}\n"
            "(archivo creado por freshness_sentinel; daily-rollup.cjs lo regenera al cierre)\n\n",
            encoding="utf-8",
        )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    cfg = json.loads((REPO_ROOT / "ops" / "freshness-sentinel.json").read_text(encoding="utf-8"))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--target",
        default=str(REPO_ROOT),
        help="Checkout a vigilar (default: el repo que contiene este script).",
    )
    parser.add_argument(
        "--rollup-dir",
        default=str(REPO_ROOT.parent / "_intel" / "rollups"),
        help="Directorio de rollups donde apendear el aviso.",
    )
    args = parser.parse_args(argv)
    target = Path(args.target)

    try:
        behind, branch = commits_behind(target)
    except RuntimeError as exc:
        print(f"freshness-sentinel: setup roto, no se pudo medir: {exc}", file=sys.stderr)
        return 2

    if behind == 0:
        print(f"freshness-sentinel: '{branch}' al dia con origin/main — ok, sin aviso.")
        return 0

    line = (
        f"- [freshness-sentinel] canonico memorymaster: '{branch}' esta {behind} "
        f"commit(s) detras de origin/main — schtask {cfg['task_name']}, "
        f"fuente {cfg['runner']}"
    )
    path = append_notice(Path(args.rollup_dir), line)
    print(f"freshness-sentinel: DIVERGENCIA — {behind} detras. Aviso en {path}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
