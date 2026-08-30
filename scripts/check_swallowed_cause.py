"""Un `except` que escribe un codigo de error constante tiene que guardar la causa.

POR QUE EXISTE. El mismo defecto aparecio TRES veces en este repo, y las tres
costo dias:

1. `profile/engine.py` registraba `AntigravityError` —un nombre de proveedor—
   cuando el fallo real era `ProfileValidationError: profile candidates must
   appear exactly once`. Diez dias de diagnostico apuntando a una caida de
   proveedor que no existia. Se descubrio recien al agregar un campo `detail`.
2. `graph_observation_engine.py` tenia `except Exception:` sin capturar y
   escribia `error_code="synthesis_failed"` fijo. La razon real —un proveedor
   dado de baja— quedaba destruida en cada uno de los 5 intentos.
3. La migracion 0022 documenta lo mismo para `outcome`: "el unico rastro de
   *por que* era un sha256 de los codigos de diagnostico — la razon se destruia
   al escribir".

La regla: si dentro de un handler de excepcion se pasa un STRING LITERAL a un
parametro que nombra la causa (`error_code`, `error`, `reason`, `outcome`), el
handler tiene que ligar la excepcion con `as` Y usarla —loguearla, guardarla en
un campo `detail`, lo que sea—. Escribir una etiqueta constante y tirar la
excepcion convierte un diagnostico de un minuto en uno de diez dias.

QUE **NO** MARCA, a proposito. Un handler que liga y usa la excepcion pasa,
aunque escriba una etiqueta constante: la etiqueta es para clasificar y el
detalle para diagnosticar, y los dos juntos estan bien. Un `raise` tambien pasa:
propagar preserva la causa por definicion. Una regla que dispara sobre codigo
que ya cumple entrena a apaciguarla, que es peor que no tenerla.

Escape: `# swallow-ok: <motivo>` en la linea del `except`.

Uso: python scripts/check_swallowed_cause.py [paths...]
Sale 1 y lista las violaciones; sale 0 y calla si no hay.
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

# Parametros que NOMBRAN la causa. Deliberadamente corto: `status` y `code`
# quedan afuera porque se usan para mil cosas que no son diagnostico, y marcarlos
# haria que la regla dispare sobre codigo sano.
CAUSE_KEYWORDS = frozenset({"error_code", "error", "reason", "outcome"})

ESCAPE = "swallow-ok"


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    keyword: str
    value: str

    def __str__(self) -> str:
        return (
            f"{self.path}:{self.line}: escribe {self.keyword}={self.value!r} dentro de un"
            f" except que descarta la excepcion"
        )


def _uses_name(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(sub, ast.Name) and sub.id == name for sub in ast.walk(node)
    )


def _constant_cause_writes(handler: ast.ExceptHandler) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(handler):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg in CAUSE_KEYWORDS and isinstance(kw.value, ast.Constant):
                if isinstance(kw.value.value, str):
                    found.append((node.lineno, kw.arg, kw.value.value))
    return found


def check_source(source: str, path: str) -> list[Violation]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()
    violations: list[Violation] = []

    for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)):
        raw = lines[handler.lineno - 1] if handler.lineno <= len(lines) else ""
        if ESCAPE in raw:
            continue
        # Re-lanzar preserva la causa por definicion.
        if any(isinstance(n, ast.Raise) for n in ast.walk(handler)):
            continue
        # Liga la excepcion Y la usa -> cumple, aunque escriba una etiqueta fija.
        if handler.name and any(
            _uses_name(stmt, handler.name) for stmt in handler.body
        ):
            continue
        for line, keyword, value in _constant_cause_writes(handler):
            violations.append(Violation(path, line, keyword, value))
    return violations


def check_paths(paths: list[Path]) -> list[Violation]:
    violations: list[Violation] = []
    for root in paths:
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for file in files:
            if "test" in file.name or "/tests/" in file.as_posix():
                continue
            violations.extend(
                check_source(file.read_text(encoding="utf-8", errors="replace"),
                             file.as_posix())
            )
    return violations


def main(argv: list[str]) -> int:
    roots = [Path(a) for a in argv[1:]] or [Path("memorymaster")]
    violations = check_paths(roots)
    if not violations:
        return 0
    print(f"{len(violations)} handler(es) escriben una causa constante y tiran la real:")
    for violation in violations:
        print(f"  {violation}")
    print(
        "\nLigar la excepcion (`except X as exc`) y usarla: loguearla o guardarla"
        f"\nen un campo de detalle. Escape justificado: `# {ESCAPE}: <motivo>`."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
