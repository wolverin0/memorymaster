"""Prueba de mutacion del guard de causa tragada (scripts/check_swallowed_cause.py).

Un test que pasa no prueba nada por si solo: prueba que el codigo actual no lo
rompe. La pregunta util es la inversa — si rompo el guard a proposito, ¿alguien
se entera? Cada mutacion de abajo desactiva UNA decision del guard. Si el test
sigue verde, esa decision no esta cubierta y el guard miente sobre su alcance.

Uso: python mutar_guard.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

MM = Path(__file__).resolve().parents[1]
GUARD = MM / "scripts" / "check_swallowed_cause.py"
TEST = "tests/test_swallowed_cause_check.py"

# (nombre, viejo, nuevo) — cada uno desactiva una decision distinta
MUTACIONES = [
    (
        "no exime el re-raise",
        "        if any(isinstance(n, ast.Raise) for n in ast.walk(handler)):\n            continue\n",
        "",
    ),
    (
        "ignora el escape swallow-ok",
        "        if ESCAPE in raw:\n            continue\n",
        "",
    ),
    (
        "no exime al que liga Y usa la excepcion",
        "        if handler.name and any(\n"
        "            _uses_name(stmt, handler.name) for stmt in handler.body\n"
        "        ):\n            continue\n",
        "",
    ),
    (
        "alcanza con ligar, sin usar (el bug que costo 10 dias)",
        "        if handler.name and any(\n"
        "            _uses_name(stmt, handler.name) for stmt in handler.body\n"
        "        ):\n            continue\n",
        "        if handler.name:\n            continue\n",
    ),
    (
        "vocabulario ancho: agrega 'status' y 'code'",
        'CAUSE_KEYWORDS = frozenset({"error_code", "error", "reason", "outcome"})',
        'CAUSE_KEYWORDS = frozenset({"error_code", "error", "reason", "outcome", "status", "code"})',
    ),
    (
        "marca tambien valores no-constantes",
        "            if kw.arg in CAUSE_KEYWORDS and isinstance(kw.value, ast.Constant):",
        "            if kw.arg in CAUSE_KEYWORDS:",
    ),
]


def main() -> int:
    original = GUARD.read_text(encoding="utf-8")
    sobrevivientes = []
    try:
        for nombre, viejo, nuevo in MUTACIONES:
            if viejo not in original:
                print(f"  ?? {nombre}: patron no encontrado, mutacion NO aplicada")
                sobrevivientes.append(nombre + " (no aplicada)")
                continue
            GUARD.write_text(original.replace(viejo, nuevo, 1), encoding="utf-8")
            res = subprocess.run(
                [sys.executable, "-m", "pytest", TEST, "-q", "--tb=no"],
                cwd=MM, capture_output=True, text=True, timeout=300,
            )
            muerta = res.returncode != 0
            fallos = [
                linea for linea in res.stdout.splitlines()
                if linea.startswith("FAILED")
            ]
            print(f"  {'MUERTA ' if muerta else 'SOBREVIVE'} {nombre}")
            if muerta:
                for f in fallos[:2]:
                    print(f"      -> {f.split('::')[-1]}")
            else:
                sobrevivientes.append(nombre)
    finally:
        GUARD.write_text(original, encoding="utf-8")

    print()
    if sobrevivientes:
        print(f"{len(sobrevivientes)} mutacion(es) SOBREVIVIERON — el guard tiene")
        print("decisiones sin cubrir:")
        for s in sobrevivientes:
            print(f"  - {s}")
        return 1
    print(f"las {len(MUTACIONES)} mutaciones murieron: cada decision del guard")
    print("tiene al menos un test que la sostiene.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
