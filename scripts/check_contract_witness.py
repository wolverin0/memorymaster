#!/usr/bin/env python3
"""Falla si un PR cambia comportamiento sin tocar ningun test.

POR QUE EXISTE. El 2026-08-28/29 el mismo defecto aparecio TRES veces en un dia:
una rama cambia un contrato y deja intacto el test que vigilaba el contrato
anterior. Las tres instancias, todas medidas:

    cola del dashboard   se redefinio a "solo del operador" y
                         test_dashboard_data_endpoints seguia exigiendo >=1 fila
    freeze de Qdrant     se congelaron las escrituras y test_qdrant_backend
                         seguia asertando que embebe y hace PUT (main 4 corridas
                         en rojo sin que nadie lo viera)
    lote del perfil      el default quedo 6,7x por encima del tope del cliente
                         y ningun test comparaba los dos numeros

EL SINTOMA MIENTE, y por eso duele. El test rojo acusa al codigo nuevo cuando el
codigo esta bien y el testigo quedo viejo; y si el test NO corre (marcador `ml`,
archivo excluido), el contrato cambia sin que nada lo note hasta semanas despues.

QUE VIGILA, exactamente: que un PR que toca `memorymaster/**.py` toque tambien
algun archivo bajo `tests/`. Es una heuristica, no una prueba: no puede saber si
el test que tocaste es el testigo correcto. Lo que si garantiza es que nadie
cambie comportamiento sin ABRIR la carpeta de tests, que es donde vive la
pregunta "y quien vigilaba esto?".

ESCAPE EXPLICITO, no silencioso. Un PR que legitimamente no necesita test —
renombrar una variable, corregir un comentario, mover un archivo— pasa poniendo
`[no-witness]` en el mensaje de algun commit del rango, con el motivo al lado.
Queda escrito en el historial, que es el punto: la excepcion se declara, no se
esconde.

Uso:
    python scripts/check_contract_witness.py --base origin/main
"""
from __future__ import annotations

import argparse
import subprocess
import sys

SOURCE_PREFIX = "memorymaster/"
TEST_PREFIX = "tests/"
ESCAPE_MARKER = "[no-witness]"

# Cambios que no pueden alterar comportamiento observable.
IGNORED_SUFFIXES = (".md", ".txt", ".rst", ".json", ".yml", ".yaml", ".toml", ".cfg", ".ini")


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} fallo: {result.stderr.strip()[:200]}")
    return result.stdout


def changed_files(base: str) -> list[str]:
    out = _git("diff", "--name-only", f"{base}...HEAD")
    return [line.strip() for line in out.splitlines() if line.strip()]


def commit_messages(base: str) -> str:
    return _git("log", "--format=%B", f"{base}..HEAD")


def orphaned_witnesses(source: list[str], changed: set[str], existing: set[str]) -> list[tuple[str, str]]:
    """Modulos cambiados cuyo test HOMONIMO existe y quedo sin tocar.

    Esta es la regla que importa, y la aprendi fallando: la primera version solo
    preguntaba "tocaste algun test?", y el PR 233 —el que dejo main rojo cuatro
    corridas— la pasaba, porque traia DIECISEIS archivos de test nuevos. Ninguno
    era el testigo del contrato que cambio: `qdrant_backend.py` se modifico y
    `tests/test_qdrant_backend.py` quedo intacto asertando el comportamiento
    anterior. Contar tests no sirve; hay que mirar CUAL.
    """
    orphans: list[tuple[str, str]] = []
    for path in source:
        module = path.rsplit("/", 1)[-1][: -len(".py")]
        witness = f"{TEST_PREFIX}test_{module}.py"
        if witness in existing and witness not in changed:
            orphans.append((path, witness))
    return orphans


def evaluate(
    files: list[str], messages: str, existing_tests: set[str] | None = None
) -> tuple[bool, str]:
    """(ok, motivo). Separado del IO para poder testearlo con diffs reales."""
    if ESCAPE_MARKER in messages:
        return True, f"exceptuado con {ESCAPE_MARKER} en un commit del rango"

    source = [
        f for f in files
        if f.startswith(SOURCE_PREFIX) and f.endswith(".py")
        and not f.endswith(IGNORED_SUFFIXES)
    ]
    if not source:
        return True, "el PR no cambia codigo de memorymaster/"

    changed = set(files)
    tests = [f for f in files if f.startswith(TEST_PREFIX)]

    orphans = orphaned_witnesses(source, changed, existing_tests or set())
    if orphans:
        listado = "\n".join(f"    {mod}  ->  {wit}  (sin tocar)" for mod, wit in orphans[:10])
        extra = f"\n    ... y {len(orphans) - 10} mas" if len(orphans) > 10 else ""
        return False, (
            f"{len(orphans)} modulo(s) cambiaron y su testigo homonimo quedo intacto:\n"
            f"{listado}{extra}\n\n"
            "Ese test asertaba el comportamiento ANTERIOR. Si el contrato cambio,\n"
            "el rojo va a aparecer despues del merge y va a acusar al codigo nuevo.\n"
            "Actualizalo, o declara la excepcion con "
            f"{ESCAPE_MARKER} y el motivo en el commit."
        )

    if tests:
        return True, f"{len(source)} archivo(s) de codigo con {len(tests)} de tests, sin testigos huerfanos"

    listado = "\n".join(f"    {f}" for f in source[:10])
    extra = f"\n    ... y {len(source) - 10} mas" if len(source) > 10 else ""
    return False, (
        f"este PR cambia {len(source)} archivo(s) de memorymaster/ y NINGUN test:\n"
        f"{listado}{extra}\n\n"
        "Un contrato que cambia sin actualizar a su testigo rompe el merge semanas\n"
        "despues, y el sintoma acusa al codigo nuevo en vez del test viejo.\n"
        "Toca el test que vigilaba el comportamiento anterior, o declara la\n"
        f"excepcion con {ESCAPE_MARKER} y el motivo en el mensaje del commit."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args(argv)

    try:
        files = changed_files(args.base)
        messages = commit_messages(args.base)
        existing = {
            line.strip()
            for line in _git("ls-files", f"{TEST_PREFIX}*.py").splitlines()
            if line.strip()
        }
    except RuntimeError as exc:
        # Una base irreconocible es un problema de setup, no un PR sin testigo.
        # Fallar aca acusaria al autor de algo que no hizo.
        print(f"no se pudo comparar contra {args.base}: {exc}", file=sys.stderr)
        return 0

    ok, reason = evaluate(files, messages, existing)
    if ok:
        print(f"contract-witness: ok — {reason}")
        return 0
    print(f"contract-witness: FALLA\n\n{reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
