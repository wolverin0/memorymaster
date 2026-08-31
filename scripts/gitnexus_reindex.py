"""Reindexa GitNexus sin perder los embeddings, y lo PRUEBA despues de correr.

POR QUE EXISTE. `npx gitnexus analyze` sin `--embeddings` no deja los embeddings
como estaban: los BORRA. Hoy son 12.079 y regenerarlos es caro. El PostToolUse
hook ya arma bien el comando (agrega el flag cuando detecta embeddings), pero los
docs del repo — `AGENTS.md` y `CLAUDE.md`, dentro del bloque generado
`<!-- gitnexus:start -->` — muestran el comando pelado en dos lugares. Ese bloque
lo reescribe el propio `analyze`, asi que corregirlo a mano dura hasta el proximo
reindex: no se puede arreglar editando el texto.

Lo que si sobrevive es esto: una via segura que no depende de recordar el flag, y
un chequeo POSTERIOR que compara el conteo de embeddings antes y despues. Si
bajaron, sale distinto de cero y lo dice. Recordar un flag es una esperanza;
verificar el invariante es un hecho.

Uso:
    python scripts/gitnexus_reindex.py            # reindexa preservando
    python scripts/gitnexus_reindex.py --check    # solo reporta, no corre nada
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
META = REPO / ".gitnexus" / "meta.json"


def embedding_count(meta_path: Path = META) -> int:
    """Cuantos embeddings tiene el indice hoy. 0 si no hay indice."""
    if not meta_path.is_file():
        return 0
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    stats = data.get("stats") or {}
    try:
        return int(stats.get("embeddings") or 0)
    except (TypeError, ValueError):
        return 0


def analyze_command(embeddings: int) -> list[str]:
    """El comando correcto para el estado actual del indice.

    Con embeddings existentes, `--embeddings` no es opcional: sin el, analyze
    los borra. Sin embeddings, agregarlo obligaria a generarlos, que es un
    trabajo distinto del que se pidio.
    """
    cmd = ["npx", "gitnexus", "analyze"]
    if embeddings > 0:
        cmd.append("--embeddings")
    return cmd


def verify_preserved(before: int, after: int) -> tuple[bool, str]:
    """El chequeo que hace util a este script: ¿sobrevivieron?

    No alcanza con `after > 0`: un indice que paso de 12.079 a 3 esta roto
    igual, y "hay algunos" lo taparia.
    """
    if before == 0:
        return True, f"no habia embeddings que preservar (after={after})"
    if after < before:
        return False, (
            f"EMBEDDINGS PERDIDOS: {before} -> {after}. Analyze corrio sin"
            " --embeddings o fallo a mitad. Regenerarlos es caro."
        )
    return True, f"embeddings preservados: {before} -> {after}"


def main(argv: list[str]) -> int:
    before = embedding_count()
    cmd = analyze_command(before)
    print(f"embeddings antes: {before}")
    print(f"comando: {' '.join(cmd)}")

    if "--check" in argv[1:]:
        return 0

    result = subprocess.run(cmd, cwd=REPO, shell=(sys.platform == "win32"))
    if result.returncode != 0:
        print(f"analyze fallo con codigo {result.returncode}")
        return result.returncode

    after = embedding_count()
    ok, mensaje = verify_preserved(before, after)
    print(mensaje)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
