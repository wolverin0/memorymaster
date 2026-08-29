"""Un subsistema apagado no puede estar fallando.

QUE PASO (2026-08-29). El operador apago PPR-7 — 2 observaciones en total, las
dos archivadas — y la revision operativa siguio dando FAIL, porque
`check_graph_observations` contaba un job bloqueado sin mirar si alguien iba a
procesarlo. Apagar la funcion no silenciaba nada: el FAIL sobrevivia al apagado
y cada corrida futura lo iba a repetir para siempre.

Es la misma familia que el aviso que sale siempre: un veredicto que no puede
volverse verde deja de ser informacion.

LAS DOS RAMAS, porque una sola no prueba nada: apagado calla AUNQUE HAYA un job
bloqueado (si no, el test pasaria con una base vacia sin ejercitar nada), y
prendido sigue fallando con el mismo job (si no, habriamos silenciado el
incidente en vez del ruido).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.operations.operational_review import (
    ReviewConfig,
    Verdict,
    check_graph_observations,
)

FLAG = "MEMORYMASTER_GRAPH_OBSERVATIONS"


@pytest.fixture()
def db_con_job_bloqueado(tmp_path: Path) -> Path:
    """Base minima con UN job bloqueado — el incidente real, reducido."""
    path = tmp_path / "review.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE graph_observation_jobs ("
        "id INTEGER PRIMARY KEY, status TEXT, stage TEXT, scope TEXT, "
        "lease_expires_at TEXT, error_code TEXT)"
    )
    con.execute(
        "INSERT INTO graph_observation_jobs (id, status, stage, scope, error_code) "
        "VALUES (8924, 'blocked', 'synthesize', 'user', 'synthesis_failed')"
    )
    con.execute("CREATE TABLE graph_observation_supports (id INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE graph_observations (observation_claim_id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    return path


def test_disabled_subsystem_is_not_reported_as_failing(db_con_job_bloqueado, monkeypatch):
    monkeypatch.setenv(FLAG, "0")
    result = check_graph_observations(ReviewConfig(db=db_con_job_bloqueado))
    assert result.verdict is Verdict.PASS
    assert "deshabilitado" in result.detail, "el veredicto tiene que DECIR por que no evaluo"


def test_enabled_subsystem_still_fails_on_the_same_blocked_job(db_con_job_bloqueado, monkeypatch):
    """Contra-caso: no silenciamos el incidente, silenciamos el ruido."""
    monkeypatch.setenv(FLAG, "1")
    result = check_graph_observations(ReviewConfig(db=db_con_job_bloqueado))
    assert result.verdict is Verdict.FAIL
