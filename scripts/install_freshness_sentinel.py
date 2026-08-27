#!/usr/bin/env python3
"""Registra el schtask del freshness sentinel desde ops/freshness-sentinel.json.

Config-by-file (T-0241): el nombre y horario viven en el repo; este instalador
solo materializa eso en el Task Scheduler. Re-ejecutar es idempotente (/f).
Correr desde el checkout CANONICO: la task apunta al wrapper de ESTE checkout.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    cfg = json.loads((REPO_ROOT / "ops" / "freshness-sentinel.json").read_text(encoding="utf-8"))
    wrapper = REPO_ROOT / cfg["wrapper"]
    if not wrapper.exists():
        print(f"wrapper no encontrado: {wrapper}", file=sys.stderr)
        return 1
    sched = cfg["schedule"]
    if sched["type"] != "daily":
        print(f"schedule.type no soportado: {sched['type']}", file=sys.stderr)
        return 1
    cmd = [
        "schtasks", "/create", "/f",
        "/tn", cfg["task_name"],
        "/tr", f'"{wrapper}"',
        "/sc", "daily",
        "/st", sched["time"],
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True)
    out = (completed.stdout or "") + (completed.stderr or "")
    print(out.strip())
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
