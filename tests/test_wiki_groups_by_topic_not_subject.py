"""El tema de la wiki vive en `topic`, no en `subject`, y por una razon dura.

`subject` es el sujeto de una tripleta y TRES mecanismos lo tratan como
identidad: el indice unico (tenant, subject, predicate, scope) sobre confirmadas
publicas, `auto_resolver` y `conflict_resolver` — estos dos leen dos claims con
el mismo (subject, predicate) y distinto object_value como una CONTRADICCION y
superseden una. Un tema de wiki agrupa decenas de claims, asi que escribir el
tema en `subject` hacia que el steward archivara claims no relacionadas entre si
en el siguiente ciclo. Medido antes de escribir nada: 176 colisiones directas del
indice unico sobre 13.767 asignaciones, y esas eran solo las que la base frenaba.

Estos tests anclan el REQUISITO ("un tema agrupa muchas claims sin que el sistema
las lea como contradictorias"), no la implementacion: siguen valiendo si cambia
como se hace la consulta, y fallan si alguien vuelve a poner el tema en `subject`
o le agrega unicidad a `topic`.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.core.service import MemoryService
from memorymaster.knowledge.wiki_engine import _load_claims_by_topic

BASE = (
    "INSERT INTO claims (id, text, claim_type, subject, topic, predicate,"
    " object_value, scope, status, pinned, confidence, created_at, updated_at,"
    " volatility, tier, version, visibility)"
    " VALUES (?,?,'fact',?,?,?,?,'project:mm','confirmed',1,0.9,"
    "'2026-08-30T00:00:00+00:00','2026-08-30T00:00:00+00:00','low','core',1,'public')"
)


@pytest.fixture()
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "wiki.db")
    MemoryService(path, workspace_root=str(tmp_path)).init_db()
    return path


def _insert(db_path: str, rows: list[tuple]) -> None:
    conn = sqlite3.connect(db_path)
    conn.executemany(BASE, rows)
    conn.commit()
    conn.close()


def test_el_tema_agrupa_claims_con_sujetos_distintos(db: str):
    """Lo que la wiki necesita: un articulo por tema, no uno por sujeto."""
    _insert(db, [
        (1, "el sentinel corre programado", "MM-freshness-sentinel", "operacion", "corre", "si"),
        (2, "el steward corre cada 6h", "MM-steward", "operacion", "corre", "si"),
        (3, "el digest sale los lunes", "MM-digest", "operacion", "sale", "lunes"),
    ])
    temas = _load_claims_by_topic(db)
    assert set(temas) == {"operacion"}, f"no agrupo por tema: {sorted(temas)}"
    assert {c["id"] for c in temas["operacion"]} == {1, 2, 3}


def test_sin_tema_cae_en_subject(db: str):
    """Backward compat: las claims viejas no tienen `topic` y deben seguir saliendo."""
    _insert(db, [
        (1, "una claim vieja sin tema asignado", "vault-curado", None, "es", "vieja"),
        (2, "otra claim vieja con tema vacio", "ruido", "", "es", "vieja"),
    ])
    temas = _load_claims_by_topic(db)
    assert set(temas) == {"vault-curado", "ruido"}, sorted(temas)


def test_el_tema_gana_cuando_estan_los_dos(db: str):
    _insert(db, [(1, "tiene ambos", "sujeto-viejo", "tema-nuevo", "es", "x")])
    assert set(_load_claims_by_topic(db)) == {"tema-nuevo"}


def test_muchas_claims_comparten_tema_y_predicado_sin_violar_el_indice(db: str):
    """La razon de ser de la columna.

    Con el tema en `subject`, estas tres claims —mismo sujeto, mismo predicado,
    distinto object_value— violan idx_claims_public_confirmed_tuple_unique y,
    peor, son exactamente el patron que `conflict_resolver` supersede. En
    `topic` conviven, que es lo que un articulo de wiki necesita.
    """
    _insert(db, [
        (1, "primera", "sujeto-a", "gotchas de windows", "requiere", "comillas"),
        (2, "segunda", "sujeto-b", "gotchas de windows", "requiere", "rutas absolutas"),
        (3, "tercera", "sujeto-c", "gotchas de windows", "requiere", "pythonw"),
    ])
    temas = _load_claims_by_topic(db)
    assert len(temas["gotchas de windows"]) == 3


def test_topic_no_tiene_indice_unico(db: str):
    """Si alguien le agrega unicidad a `topic`, vuelve el problema que origino todo."""
    conn = sqlite3.connect(db)
    unicos = [
        sql for (sql,) in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        ) if "topic" in sql and "UNIQUE" in sql.upper()
    ]
    conn.close()
    assert not unicos, f"topic quedo con unicidad, que es justo lo que no puede tener: {unicos}"


def test_una_base_sin_la_columna_sigue_funcionando(tmp_path: Path):
    """Bases viejas, armadas a mano o llegadas por db_merge no tienen `topic`.

    El tema es una mejora, no un requisito: sin la columna el motor agrupa por
    subject como siempre. Sin esto, la wiki reventaba con
    `OperationalError: no such column: topic` sobre cualquier base no migrada.
    """
    path = str(tmp_path / "vieja.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE claims (id INTEGER PRIMARY KEY, text TEXT, claim_type TEXT,"
        " subject TEXT, predicate TEXT, object_value TEXT, scope TEXT, confidence REAL,"
        " status TEXT, human_id TEXT, created_at TEXT, updated_at TEXT, event_time TEXT,"
        " pinned INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO claims (id, text, subject, scope, status, confidence)"
        " VALUES (1, 'claim de una base sin migrar', 'tema-viejo', 'project:mm',"
        " 'confirmed', 0.9)"
    )
    conn.commit()
    conn.close()

    temas = _load_claims_by_topic(path)
    assert set(temas) == {"tema-viejo"}, sorted(temas)
