#!/usr/bin/env python3
"""Falla si un PR modifica el marcador o sus sondas.

POR QUE EN CI Y NO EN OTRO LADO. Un loop con meta numerica busca el camino mas
barato al numero, y editar la regla siempre es mas barato que arreglar el sistema:
subir un limite de corte, achicar la muestra, aflojar una tolerancia. Las capas
disponibles y lo que frena cada una:

    ledger + fleet minimum   frena CREAR la tarea      no esquivable, pero solo
                                                       aplica al trabajo por ledger
    allowed_paths del graph  no frena nada             es declarativo
    hook de pre-commit       frena el commit local     --no-verify, core.hooksPath
    ESTE CHECK EN CI         frena el MERGE            no esquivable por un agente

Mergear exige CI verde, asi que esta es la unica capa que un agente trabajando
local no puede saltear.

REGLA: falla si un archivo congelado YA EXISTIA en la rama base y cambio. El alta
inicial se permite — si no, este mismo check bloquearia el PR que introduce el
marcador. Modificar despues, no.

SIN ESCAPE HATCH a proposito. No hay marcador de aprobacion en el mensaje del
commit ni etiqueta que lo saltee, porque cualquiera de esos los puede escribir el
agente que quiere saltearlo. Si hay que tocar una sonda, lo hace el operador.

CI TIENE QUE CORRER LA COPIA DE LA RAMA BASE, no la que trae el PR. El job hace
`git show origin/<base>:scripts/check_probes_frozen.py` y ejecuta ESO. Cablearlo
de la forma obvia — `python scripts/check_probes_frozen.py` sobre el checkout del
PR — deja que una rama reemplace este archivo por `sys.exit(0)` y que la version
neutralizada certifique su propio cambio, con lo cual la entrada de FROZEN que
dice "el guarda se congela a si mismo" no frena nada. El caso esta fijado en
tests/test_probes_frozen_guard_fires.py.

AGUJERO RESIDUAL, declarado en vez de tapado mal: alguien puede borrar el job de
CI que llama a este script. Queda visible en el diff del PR. No lo cierro con mas
codigo porque el codigo tambien seria borrable; se cierra mirando el diff.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

FROZEN = (
    "scripts/probe_suite.py",
    "scripts/probes/",
    "scripts/check_probes_frozen.py",  # el guarda se congela a si mismo
)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def _is_frozen(path: str) -> bool:
    return any(path == f or path.startswith(f) for f in FROZEN)


def _existed_on_base(base: str, path: str) -> bool:
    r = subprocess.run(["git", "cat-file", "-e", f"{base}:{path}"], capture_output=True)
    return r.returncode == 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="origin/main", help="rama contra la que se compara")
    args = ap.parse_args(argv)

    try:
        changed = [p for p in _git("diff", "--name-only", f"{args.base}...HEAD").splitlines() if p]
    except subprocess.CalledProcessError as exc:
        print(f"no se pudo comparar contra {args.base}: {exc.stderr.strip() or exc}", file=sys.stderr)
        print(f"probar: git fetch origin {args.base.split('/')[-1]}", file=sys.stderr)
        return 1

    violations = [p for p in changed if _is_frozen(p) and _existed_on_base(args.base, p)]
    added = [p for p in changed if _is_frozen(p) and not _existed_on_base(args.base, p)]

    if added:
        print(f"alta inicial permitida: {', '.join(added)}")

    if violations:
        print(
            "\nEL MARCADOR ESTA CONGELADO — este PR modifica:\n"
            + "".join(f"    {p}\n" for p in violations)
            + "\nUna meta numerica cuya regla puede editar quien la persigue no mide el\n"
              "sistema, mide la regla. Si el cambio es legitimo, lo aplica el operador.\n",
            file=sys.stderr,
        )
        return 1

    print("marcador intacto")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
