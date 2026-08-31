"""El reindex de GitNexus no puede borrar los embeddings, y lo prueba al final.

`npx gitnexus analyze` sin `--embeddings` los BORRA en vez de dejarlos. Son
12.079 y regenerarlos es caro. El hook de Claude Code ya arma bien el comando,
pero los docs del repo muestran el comando pelado en dos lugares — y viven
dentro del bloque `<!-- gitnexus:start -->`, que el propio analyze reescribe, asi
que corregir el texto no dura hasta el proximo reindex.

Por eso el arreglo no es documental: es una via segura que arma el comando sola
y VERIFICA el conteo despues de correr. Estos tests fijan las dos mitades — que
el flag se agregue cuando corresponde, y que la verificacion posterior detecte la
perdida en vez de dar por buena cualquier corrida que termine en 0.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.gitnexus_reindex import (
    analyze_command,
    embedding_count,
    verify_preserved,
)


# --- el comando -------------------------------------------------------------

def test_con_embeddings_el_flag_no_es_opcional():
    assert analyze_command(12079) == ["npx", "gitnexus", "analyze", "--embeddings"]


def test_sin_embeddings_no_se_agrega_el_flag():
    """Agregarlo obligaria a generarlos, que es otro trabajo del que se pidio."""
    assert analyze_command(0) == ["npx", "gitnexus", "analyze"]


# --- la verificacion posterior, que es lo que hace util al script -----------

def test_detecta_la_perdida_total():
    ok, msg = verify_preserved(12079, 0)
    assert not ok and "PERDIDOS" in msg


def test_detecta_la_perdida_PARCIAL():
    """Un indice que paso de 12.079 a 3 esta roto igual.

    Este es el caso que un chequeo ingenuo (`after > 0`) daria por bueno, y por
    eso la comparacion es contra el conteo previo y no contra cero.
    """
    ok, msg = verify_preserved(12079, 3)
    assert not ok, msg


def test_acepta_que_crezcan():
    ok, _ = verify_preserved(12074, 12079)
    assert ok


def test_acepta_que_no_hubiera_nada_que_preservar():
    ok, _ = verify_preserved(0, 0)
    assert ok


# --- lectura del meta -------------------------------------------------------

def test_lee_el_conteo_del_meta(tmp_path: Path):
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"stats": {"embeddings": 12079}}), encoding="utf-8")
    assert embedding_count(meta) == 12079


def test_meta_ausente_o_ilegible_cuenta_cero_sin_reventar(tmp_path: Path):
    """Un indice que no existe todavia no es un error: es cero."""
    assert embedding_count(tmp_path / "no-existe.json") == 0
    roto = tmp_path / "roto.json"
    roto.write_text("{no es json", encoding="utf-8")
    assert embedding_count(roto) == 0


def test_meta_sin_el_campo_cuenta_cero(tmp_path: Path):
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"stats": {}}), encoding="utf-8")
    assert embedding_count(meta) == 0
