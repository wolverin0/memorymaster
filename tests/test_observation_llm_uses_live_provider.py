"""La sintesis de observaciones no puede clavar un proveedor a mano.

`_observation_llm` forzaba `MEMORYMASTER_LLM_PROVIDER="opencode"` — el camino
GLM que se dio de baja el 2026-08-20. El consolidador migro a Antigravity ese
mismo dia (`dreaming/providers.py:build_consolidator`) y este call site quedo
atras, mandando un modelo de Antigravity a un proveedor muerto.

En produccion se veia como `opencode: provider call failed code=call_failed` dos
veces por corrida de dreaming, con `graph_observations.failed=2` al lado, y
dejaba jobs de sintesis girando en backoff hasta agotar los 5 intentos. Medido
el 2026-08-30: los mismos dos jobs corridos por el proveedor por defecto arman
componente, llaman y parsean sin error, los dos con decision=emit.

El test ancla el REQUISITO ("la sintesis usa el proveedor vigente, no uno
elegido a mano"), no la implementacion: falla si alguien vuelve a envolver la
llamada en un override de proveedor, sin importar cual elija.
"""
from __future__ import annotations

import inspect

from memorymaster.dreaming import worker as worker_module


def test_no_fija_proveedor_a_mano():
    """Un override aca es como se murio la sintesis por diez dias."""
    fuente = inspect.getsource(worker_module.DreamWorker._observation_llm)
    cuerpo = "\n".join(
        linea for linea in fuente.splitlines() if not linea.strip().startswith("#")
    )
    _, _, codigo = cuerpo.partition('"""')
    _, _, codigo = codigo.partition('"""')

    assert "MEMORYMASTER_LLM_PROVIDER" not in codigo, (
        "la sintesis volvio a clavar un proveedor; el default es el que sigue"
        " la migracion, un override queda atras y falla en silencio"
    )
    assert "use_call_scoped_env" not in codigo, (
        "envolver la llamada en env scopeado es como se colo el override"
    )


def test_sigue_llamando_al_proveedor():
    """Complemento del anterior: no vale 'arreglarlo' dejando de llamar."""
    fuente = inspect.getsource(worker_module.DreamWorker._observation_llm)
    assert "call_llm(system, prompt)" in fuente


def test_el_consolidador_por_defecto_no_es_el_camino_retirado():
    """La migracion del 2026-08-20 que este call site se habia perdido.

    Ojo con "limpiar" mas de la cuenta: `GLMConsolidator` NO es codigo muerto.
    `create_dream_consolidator` lo deja ELEGIBLE a proposito — si el plan de
    zai-coding-plan vuelve, alcanza con una variable de entorno en vez de un
    commit. Lo que estaba muerto era clavar ese proveedor en un call site que
    nadie repasa. Este test fija el DEFAULT, no borra la opcion.
    """
    from memorymaster.dreaming.providers import create_dream_consolidator

    consolidator = create_dream_consolidator()
    nombre = type(consolidator).__name__.lower()
    assert "glm" not in nombre, "el default no puede ser el plan dado de baja"
    assert "antigravity" in nombre
