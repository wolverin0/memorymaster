"""El reduce del perfil va por lotes y un run a medias es reanudable, no destructivo.

Por que existe: el reduce pasaba TODOS los candidatos de un run en una sola
llamada contra un validador que exige particion perfecta (cada candidate_id
exactamente una vez). El run 2 completo con 68 candidatos; el run 3 acumulo 234
y quedo clavado en `reducing` desde el 2026-08-20 — diez dias de corridas
programadas mas cuatro reintentos medidos a mano, todos fallando entre
`profile candidates must appear exactly once` y JSON malformado. El perfil que
se inyecta en cada SessionStart quedo con hechos del 6 al 9 de agosto.

Lotear sin marcar consumidos habria sido PEOR que el bloqueo: `apply_decisions`
relee los candidatos del run en cada llamada, asi que un lote aplicado y un
crash antes del siguiente dejaba los mismos candidatos listos para aplicarse de
nuevo. Por eso el test que manda aca es el de reanudacion.

Los tests anclan el REQUISITO, no la implementacion: siguen valiendo si cambia
el tamano de lote o como se arma la consulta, y fallan si alguien vuelve a
mandar todo junto o saca el marcado de la transaccion.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memorymaster.profile.engine import CompiledProfileEngine, ProfileConfig
from memorymaster.profile.models import ProfileDecision
from memorymaster.profile.repository import ProfileRepository

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def repo(tmp_path: Path) -> ProfileRepository:
    from memorymaster.core.service import MemoryService

    db = tmp_path / "memory.db"
    MemoryService(db, workspace_root=tmp_path).init_db()
    repo = ProfileRepository(db)
    repo._test_db = db
    return repo


def _seed(repo: ProfileRepository, run_id: int, count: int) -> None:
    """Siembra un run en `reducing` con `count` candidatos pendientes.

    Sin `OR IGNORE` a proposito: `volatility` tiene CHECK y `run_id` tiene FK,
    asi que un seed mal armado debe REVENTAR aca y no producir cero filas en
    silencio, que fue exactamente como se escondio el primer intento de este
    test.
    """
    conn = sqlite3.connect(str(repo._test_db))
    conn.execute(
        """INSERT INTO compiled_profile_runs
             (id, status, start_watermark, current_watermark, target_watermark,
              map_model, reduce_model, started_at, updated_at)
           VALUES (?,'reducing',0,0,0,'m','r',?,?)""",
        (run_id, NOW.isoformat(), NOW.isoformat()),
    )
    conn.executemany(
        """INSERT INTO compiled_profile_candidates
             (run_id, candidate_id, category, predicate, value, volatility,
              support_ids_json, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        [
            (run_id, f"c{i}", "identity_locale", "location", f"valor {i}",
             "stable", f"[{i}]", NOW.isoformat())
            for i in range(count)
        ],
    )
    conn.commit()
    conn.close()


def test_pending_only_excluye_los_ya_consumidos(repo: ProfileRepository):
    """La pieza que hace reanudable un run a medias."""
    _seed(repo, 1, 5)
    assert len(repo.candidates(1, pending_only=True)) == 5

    repo.apply_decisions(
        1,
        (ProfileDecision(candidate_ids=("c0", "c1"), action="ignore"),),
        now=NOW,
        min_sessions=1,
    )

    pendientes = {c.candidate_id for c in repo.candidates(1, pending_only=True)}
    assert pendientes == {"c2", "c3", "c4"}, f"no filtro los consumidos: {pendientes}"
    assert len(repo.candidates(1)) == 5, "sin pending_only debe seguir viendo todos"


def test_reaplicar_el_mismo_lote_no_lo_consume_dos_veces(repo: ProfileRepository):
    """Un crash entre lotes no puede traducirse en hechos duplicados."""
    _seed(repo, 1, 3)
    decision = (ProfileDecision(candidate_ids=("c0",), action="ignore"),)

    primera = repo.apply_decisions(1, decision, now=NOW, min_sessions=1)
    segunda = repo.apply_decisions(1, decision, now=NOW, min_sessions=1)

    conn = sqlite3.connect(str(repo._test_db))
    marcas = conn.execute(
        "SELECT consumed_at FROM compiled_profile_candidates"
        " WHERE run_id=1 AND candidate_id='c0'"
    ).fetchall()
    conn.close()
    assert len(marcas) == 1 and marcas[0][0] is not None
    assert primera["consumed"] == 1
    assert segunda["consumed"] == 1, (
        "el contador cuenta el intento; lo que no puede pasar es que la fila"
        " cambie de consumed_at, y el UPDATE la protege con IS NULL"
    )


def test_el_reduce_trocea_en_lotes_acotados(repo: ProfileRepository, tmp_path: Path):
    """234 candidatos NO pueden irse en una sola llamada al modelo."""
    _seed(repo, 1, 234)
    vistos: list[int] = []

    class Reducer:
        model = "fake"

        def reduce(self, candidates, facts):
            vistos.append(len(candidates))
            return (
                ProfileDecision(
                    candidate_ids=tuple(c.candidate_id for c in candidates),
                    action="ignore",
                ),
            )

    engine = CompiledProfileEngine(
        repo, None, Reducer(),
        output_dir=tmp_path / "proj",
        config=ProfileConfig(reduce_batch_size=40),
    )
    engine._reduce_and_complete(1, NOW)

    assert max(vistos) <= 40, f"un lote se paso del tope: {max(vistos)}"
    assert sum(vistos) == 234, f"no cubrio todos los candidatos: {sum(vistos)}"
    assert len(vistos) == 6, f"esperaba 6 lotes (234/40), hubo {len(vistos)}"
    assert not repo.candidates(1, pending_only=True), "quedaron candidatos sin consumir"


def test_un_lote_que_no_consume_nada_corta_en_vez_de_loopear(
    repo: ProfileRepository, tmp_path: Path
):
    """Sin este corte, un reducer que devuelve vacio gira para siempre."""
    _seed(repo, 1, 10)

    class ReducerVacio:
        model = "fake"

        def reduce(self, candidates, facts):
            return ()

    engine = CompiledProfileEngine(
        repo, None, ReducerVacio(),
        output_dir=tmp_path / "proj",
        config=ProfileConfig(reduce_batch_size=5),
    )
    from memorymaster.profile.models import ProfileValidationError

    with pytest.raises(ProfileValidationError, match="no consumio ninguno"):
        engine._reduce_and_complete(1, NOW)
