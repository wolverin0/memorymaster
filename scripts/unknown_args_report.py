#!/usr/bin/env python3
"""Leer que parametros inventados llegan de verdad a las herramientas MCP.

ESTE SCRIPT ES LA MITAD QUE IMPIDE QUE LA MEDICION SEA INERTE. El contador de
`surfaces/unknown_args.py` acumula evidencia; sin un lector, su silencio es
indistinguible de "nadie pasa parametros de mas" — que es exactamente la familia
de bug que la medicion existe para cerrar.

PARA QUE SIRVE. Las 51 herramientas MCP aceptan argumentos que no declaran y los
descartan calladas. Endurecerlas de golpe con `additionalProperties: false`
convierte en error lo que hoy se ignora y romperia sin aviso a llamadores que
funcionan por accidente. La decision del operador fue medir primero. Este
reporte es lo que se consulta para decidir.

COMO LEERLO:
  - Filas con un nombre PARECIDO a un parametro real (`scope` donde va
    `scope_allowlist`, `dry_run` donde va `apply`) son llamadores rotos hoy:
    creen que filtran y no filtran. Son la razon del endurecimiento.
  - Filas con nombres ajenos al dominio suelen ser clientes que mandan metadata
    extra. Endurecer los romperia sin que hubiera nada roto.

USO:
    python scripts/unknown_args_report.py [ruta-al-jsonl]

Sin argumento usa MEMORYMASTER_UNKNOWN_ARGS_LOG.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path


def main(argv: list[str]) -> int:
    raw = argv[1] if len(argv) > 1 else os.environ.get("MEMORYMASTER_UNKNOWN_ARGS_LOG", "").strip()
    if not raw:
        print(
            "No hay log configurado. Exporta MEMORYMASTER_UNKNOWN_ARGS_LOG con una ruta\n"
            "y dejalo correr sobre trafico real antes de decidir el endurecimiento.",
            file=sys.stderr,
        )
        return 2

    ruta = Path(raw)
    if not ruta.exists():
        print(
            f"El log {ruta} no existe todavia.\n"
            "OJO: eso NO significa que nadie pase parametros de mas — significa que\n"
            "todavia no se midio. No lo leas como evidencia de que endurecer es seguro.",
            file=sys.stderr,
        )
        return 2

    pares: Counter[tuple[str, str]] = Counter()
    por_tool: Counter[str] = Counter()
    lineas = malformadas = 0
    with ruta.open(encoding="utf-8") as fh:
        for linea in fh:
            linea = linea.strip()
            if not linea:
                continue
            lineas += 1
            try:
                registro = json.loads(linea)
                tool = str(registro["tool"])
                for arg in registro.get("unknown", []):
                    pares[(tool, str(arg))] += 1
                    por_tool[tool] += 1
            except (ValueError, KeyError, TypeError):
                malformadas += 1

    if not pares:
        print(f"{lineas} registros leidos, ningun parametro desconocido.")
        print("Con trafico real suficiente, esto SI es evidencia de que endurecer es seguro.")
        return 0

    print(f"=== parametros inventados: {sum(pares.values())} en {lineas} registros ===\n")
    print(f"{'herramienta':<28} {'argumento':<24} {'veces':>6}")
    print("-" * 60)
    for (tool, arg), n in pares.most_common():
        print(f"{tool:<28} {arg:<24} {n:>6}")

    print(f"\n=== por herramienta ===")
    for tool, n in por_tool.most_common():
        print(f"  {tool:<28} {n:>6}")

    if malformadas:
        print(f"\n{malformadas} lineas malformadas y salteadas.")

    print(
        "\nAntes de endurecer: revisa fila por fila si el nombre es un TIPEO de un\n"
        "parametro real (llamador roto hoy, endurecer lo ayuda) o metadata ajena\n"
        "(llamador sano hoy, endurecer lo rompe)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
