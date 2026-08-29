"""`wiki-absorb --pinned-only`: la proyeccion curada no puede volver a ser el vault de 2 GB.

POR QUE EXISTE ESTE MODO (2026-08-26). El vault se retiro en julio (claim 104145)
con 2 GB y 5.921 articulos: absorber sin gate espeja la base entera, que es lo
contrario del patron llm-wiki ("cientos de paginas que entran en contexto").
Medido antes de elegir el gate: las senales automaticas no discriminan —
decisiones confirmadas 5.174, tier=core 9.587, union 13.069 (65x el techo).
La UNICA senal que significa "alguien eligio esto" es `pinned` (6 al medir).

EL TEST QUE MAS IMPORTA ES EL CONTROL NEGATIVO
(`test_without_the_flag_everything_still_flows`): un filtro cableado al reves
—que siempre filtrara, o que nunca filtrara— pasa cualquier test que solo mire
un lado. Y `test_absorb_with_no_pinned_claims_writes_nothing` ejercita la
cadena completa absorb -> _absorb_impl -> _load_claims_by_topic sin tocar el
LLM, porque cero sujetos = cero llamadas: si el flag se pierde en el camino
(el bug clasico de parametro que se evapora), ese test escribe articulos y
falla.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.core.service import MemoryService
from memorymaster.knowledge.wiki_engine import _load_claims_by_topic, absorb


@pytest.fixture()
def db_con_claims(tmp_path: Path) -> str:
    """Base real via init_db, con dos claims confirmadas: una pineada y una no."""
    db = str(tmp_path / "wiki.db")
    MemoryService(db, workspace_root=str(tmp_path)).init_db()
    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT INTO claims (id, text, claim_type, subject, scope, status, pinned,
             confidence, created_at, updated_at, volatility, tier, version, visibility)
           VALUES (1, 'decision pineada: el vault curado se proyecta solo desde pinned',
             'decision', 'vault-curado', 'project:memorymaster', 'confirmed', 1,
             0.9, '2026-08-26T00:00:00+00:00', '2026-08-26T00:00:00+00:00',
             'low', 'core', 1, 'public')"""
    )
    conn.execute(
        """INSERT INTO claims (id, text, claim_type, subject, scope, status, pinned,
             confidence, created_at, updated_at, volatility, tier, version, visibility)
           VALUES (2, 'ruido confirmado: una claim mas del monton que no fue elegida',
             'fact', 'ruido', 'project:memorymaster', 'confirmed', 0,
             0.9, '2026-08-26T00:00:00+00:00', '2026-08-26T00:00:00+00:00',
             'low', 'core', 1, 'public')"""
    )
    conn.commit()
    conn.close()
    return db


# --- el gate en el loader --------------------------------------------------

def test_pinned_only_excludes_the_unpinned(db_con_claims: str):
    temas = _load_claims_by_topic(db_con_claims, pinned_only=True)
    ids = {c["id"] for claims in temas.values() for c in claims}
    assert ids == {1}, f"el gate dejo pasar claims no pineadas: {ids}"


def test_without_the_flag_everything_still_flows(db_con_claims: str):
    """Control negativo del cableado: el default NO filtra.

    Un filtro invertido (siempre activo) pasaria el test de arriba y rompería
    el absorb normal en silencio.
    """
    temas = _load_claims_by_topic(db_con_claims)
    ids = {c["id"] for claims in temas.values() for c in claims}
    assert ids == {1, 2}


def test_tier_core_is_not_a_substitute_for_pinning(db_con_claims: str):
    """La claim de ruido es tier=core A PROPOSITO y aun asi queda afuera.

    Es la medicion que motivo el gate: tier=core tiene 9.587 claims — si el
    filtro aceptara core, el vault curado seria el vault de 2 GB de nuevo.
    """
    temas = _load_claims_by_topic(db_con_claims, pinned_only=True)
    ids = {c["id"] for claims in temas.values() for c in claims}
    assert 2 not in ids


# --- la cadena completa, sin LLM -------------------------------------------

def test_absorb_with_no_pinned_claims_writes_nothing(tmp_path: Path):
    """Si el parametro se evapora en absorb -> _absorb_impl -> loader, esto falla.

    Cero claims pineadas => cero sujetos => cero articulos y cero llamadas LLM.
    Con el bug del parametro perdido, absorberia la claim confirmada normal y
    escribiria un articulo.
    """
    db = str(tmp_path / "wiki2.db")
    MemoryService(db, workspace_root=str(tmp_path)).init_db()
    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT INTO claims (id, text, claim_type, subject, scope, status, pinned,
             confidence, created_at, updated_at, volatility, tier, version, visibility)
           VALUES (7, 'confirmada sin pinear que NO debe llegar al vault curado',
             'fact', 'no-elegida', 'project:x', 'confirmed', 0,
             0.9, '2026-08-26T00:00:00+00:00', '2026-08-26T00:00:00+00:00',
             'low', 'working', 1, 'public')"""
    )
    conn.commit(); conn.close()

    salida = tmp_path / "vault"
    resultado = absorb(db, salida, pinned_only=True)

    assert resultado.get("subjects", -1) == 0, resultado
    escritos = list(salida.rglob("*.md"))
    assert not [p for p in escritos if p.name not in ("index.md",)], (
        f"el vault curado absorbio claims sin pinear: {[p.name for p in escritos]}"
    )


# --- el CLI expone el flag -------------------------------------------------

def test_the_cli_parser_accepts_pinned_only():
    from memorymaster.surfaces.cli import build_parser

    args = build_parser().parse_args(["wiki-absorb", "--pinned-only", "--no-bases"])
    assert args.pinned_only is True


def test_the_cli_default_is_off():
    from memorymaster.surfaces.cli import build_parser

    args = build_parser().parse_args(["wiki-absorb"])
    assert args.pinned_only is False
