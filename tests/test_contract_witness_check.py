"""El guard del testigo se prueba en sus DOS ramas, y sobre todo en el silencio.

Un guard que ladra en PRs correctos es PEOR que no tenerlo: entrena a agregar el
escape por reflejo y a dejar de leerlo. Por eso la mitad de estos casos verifica
que NO dispara — PRs de solo tests, de solo documentacion, de solo scripts.

Los tres casos de `test_the_three_real_incidents_would_have_fired` no son
inventados: son las formas exactas de los tres PRs del 2026-08-28/29 que
cambiaron un contrato sin tocar a su testigo.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_contract_witness import ESCAPE_MARKER, evaluate  # noqa: E402

# Testigos que existen en el repo; se declaran explicitos para que el test no
# dependa del arbol de trabajo y siga significando lo mismo dentro de un anio.
TESTIGOS = {
    "tests/test_qdrant_backend.py",
    "tests/test_dashboard.py",
    "tests/test_review.py",
    "tests/test_service.py",
}


# --- dispara cuando tiene que disparar ------------------------------------

def test_source_change_without_any_test_fails():
    ok, reason = evaluate(["memorymaster/govern/review.py"], "fix: cambia la cola", set())
    assert not ok
    assert "NINGUN test" in reason


def test_the_qdrant_freeze_would_have_been_caught():
    """La forma REAL del PR 233, que dejo main rojo cuatro corridas.

    Este es el caso que la primera version de este guard NO detectaba: el PR
    traia dieciseis archivos de test nuevos, asi que "tocaste tests?" daba que
    si, mientras `tests/test_qdrant_backend.py` quedaba intacto asertando que el
    backend escribe — justo lo que el freeze acababa de apagar.
    """
    archivos = [
        "memorymaster/recall/qdrant_backend.py",
        "memorymaster/govern/review.py",
        "tests/test_qdrant_writes_are_frozen.py",   # test NUEVO
        "tests/test_review.py",                      # testigo tocado, ok
        "tests/test_dashboard.py",
    ]
    ok, reason = evaluate(archivos, "fix: freeze de qdrant", TESTIGOS)
    assert not ok, "el guard sigue sin ver el testigo huerfano"
    assert "test_qdrant_backend.py" in reason
    assert "test_review.py" not in reason, "no puede acusar a un testigo que SI se toco"


def test_it_names_the_orphaned_witness_so_it_is_actionable():
    ok, reason = evaluate(
        ["memorymaster/recall/qdrant_backend.py"], "fix", TESTIGOS
    )
    assert not ok
    assert "tests/test_qdrant_backend.py" in reason
    assert "sin tocar" in reason


def test_the_message_names_the_files_so_it_is_actionable():
    ok, reason = evaluate(["memorymaster/core/service.py"], "refactor")
    assert not ok
    assert "memorymaster/core/service.py" in reason, "sin el archivo hay que ir a buscarlo"


# --- CALLA cuando tiene que callar ----------------------------------------

def test_source_change_with_tests_passes():
    ok, _ = evaluate(
        ["memorymaster/govern/review.py", "tests/test_review.py"], "fix: con testigo"
    )
    assert ok


def test_a_docs_only_pr_is_silent():
    ok, _ = evaluate(["README.md", ".planning/ROADMAP.md", "docs/x.md"], "docs")
    assert ok


def test_a_tests_only_pr_is_silent():
    ok, _ = evaluate(["tests/test_review.py"], "test: cubre un hueco")
    assert ok


def test_a_scripts_only_pr_is_silent():
    """scripts/ no es comportamiento de la libreria; tiene sus propios guards."""
    ok, _ = evaluate(["scripts/freshness_sentinel.py"], "ops: nuevo runner")
    assert ok


def test_generated_and_config_files_under_memorymaster_do_not_count():
    ok, _ = evaluate(["memorymaster/schema.sql.md", "memorymaster/data/x.json"], "chore")
    assert ok


def test_the_escape_marker_is_honoured():
    ok, reason = evaluate(
        ["memorymaster/core/service.py"],
        f"refactor: renombra una variable local {ESCAPE_MARKER} sin cambio de comportamiento",
    )
    assert ok
    assert ESCAPE_MARKER in reason


def test_the_escape_marker_only_counts_inside_the_range():
    """Un marcador que no esta en los commits del rango no exceptua nada."""
    ok, _ = evaluate(["memorymaster/core/service.py"], "refactor: sin marcador")
    assert not ok
