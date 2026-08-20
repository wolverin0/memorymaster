"""El arnes de LongMemEval tiene que seguir siendo ejecutable, no solo existir.

POR QUE ESTE ARCHIVO. El arnes se escribio el 2026-04-23 (commit 6e62c4d), midio
500 preguntas, reporto hit@1 0,342 — y nunca llego a main. Sobrevivio en ramas
laterales cuatro meses mientras el paquete se reorganizaba debajo. Cuando se lo
fue a usar, nadie sabia si todavia corria.

Esa es la falla que este archivo vigila, y NO es "el script tiene un bug": es que
un instrumento de medicion sin nadie que lo invoque se pudre en silencio y su
ausencia es indistinguible de que todo anda bien.

QUE SE VIGILA Y POR QUE ESO. El arnes no usa la API publica: entra por SEIS
helpers privados de context_hook para rankear exactamente como rankea produccion.
Eso es deliberado y es su valor — un arnes que reimplementa el ranking mide su
propia reimplementacion — pero lo ata a firmas sin contrato de estabilidad. Un
rename inocente dentro de context_hook lo rompe sin que nada falle en ninguna
suite.

LO QUE ESTE ARCHIVO NO HACE: no corre las 500 preguntas. Bajar el dataset son
15 MB y el run completo son minutos por configuracion; eso es trabajo de una
corrida deliberada, no de cada CI. La cobertura aca es de CABLEADO — que el
instrumento siga enchufado — no de resultados.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
HARNESS = REPO / "scripts" / "run_longmemeval.py"

# Firmas de las que depende el arnes, con la cantidad de posicionales que pasa.
# Cambiar cualquiera de estas rompe la medicion; el test las convierte en contrato.
PRIVATE_HELPERS = {
    "_recall_weight": 1,
    "_entity_fanout_claim_ids": 3,
    "_row_for_claim": 1,
    "_apply_vector_fallback": 4,
    "_bm25_enabled": 0,
    "_bm25_param": 2,
}


def test_the_harness_is_on_main():
    """Existe en el arbol. La ausencia de esto es literalmente lo que paso."""
    assert HARNESS.is_file(), (
        "scripts/run_longmemeval.py no esta en el arbol. Vivio cuatro meses solo "
        "en ramas laterales; si volvio a salirse de main, este es el aviso."
    )


def test_the_harness_still_starts():
    """Invocable de punta a punta: argparse, imports de modulo, sintaxis."""
    out = subprocess.run(
        [sys.executable, str(HARNESS), "--help"],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    assert out.returncode == 0, f"el arnes no arranca:\n{out.stderr[-2000:]}"
    assert "--configs" in out.stdout


@pytest.mark.parametrize("helper,n_args", sorted(PRIVATE_HELPERS.items()))
def test_private_helper_signature_is_unchanged(helper: str, n_args: int):
    """El contrato real del arnes: seis funciones privadas de context_hook.

    Sin este test, renombrar o cambiar la firma de cualquiera de ellas pasa todas
    las suites y rompe la medicion — que es justo el defecto que no se nota.
    """
    from memorymaster.recall import context_hook as ch

    fn = getattr(ch, helper, None)
    assert fn is not None, (
        f"context_hook.{helper} ya no existe. El arnes de LongMemEval lo llama "
        f"para rankear como produccion; si se renombro, actualizar el arnes Y "
        f"esta lista en el mismo commit."
    )
    sig = inspect.signature(fn)
    positional = [
        p for p in sig.parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    required = [p for p in positional if p.default is p.empty]
    assert len(required) <= n_args <= len(positional), (
        f"context_hook.{helper}{sig} ya no acepta los {n_args} posicionales que "
        f"le pasa el arnes"
    )


def test_the_harness_does_not_lean_on_deprecated_shims():
    """Los shims del layout viejo se declaran a si mismos temporales.

    memorymaster/context_hook.py dice "keeps the old import path working for ONE
    MINOR VERSION". El arnes originalmente importaba por ahi, asi que estaba a un
    borrado de rutina de romperse — y sin nadie corriendolo, el borrado habria
    parecido inofensivo.
    """
    tree = ast.parse(HARNESS.read_text(encoding="utf-8"))
    viejos = {
        "memorymaster.models", "memorymaster.service", "memorymaster.security",
        "memorymaster.context_hook", "memorymaster.recall_tokenizer",
        "memorymaster.verbatim_recall", "memorymaster.verbatim_store",
    }
    encontrados = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in viejos:
            encontrados.add(node.module)
        elif isinstance(node, ast.ImportFrom) and node.module == "memorymaster":
            for alias in node.names:
                if f"memorymaster.{alias.name}" in viejos:
                    encontrados.add(f"memorymaster.{alias.name}")

    assert not encontrados, (
        f"el arnes importa por shims deprecados: {sorted(encontrados)}\n"
        "Usar las rutas reales (memorymaster.core.*, memorymaster.recall.*): los "
        "shims estan documentados como temporales y su borrado no va a avisar."
    )
