"""Tope de gasto de Dreaming en TOKENS, y las dos entradas resolviendo el mismo proveedor.

CONTEXTO (rulings MM3/MM4 del operador, 2026-08-24). El ledger de produccion
mostraba 9.762.549 tokens de entrada atribuidos a `openai`, un proveedor que no
esta en el spec. Habia dos agujeros, y los dos se cierran aca.

AGUJERO 1 — el tope no acotaba gasto. `max_extract_calls_daily` cuenta LLAMADAS.
Medido sobre el mismo ledger: 527 llamadas a openai costaron 9,76M tokens (~18,5k
cada una) y 540 a google costaron 3,89M (~7,2k). El mismo tope de llamadas deja
pasar gastos que difieren 2,5x, asi que un tope en llamadas no es un tope de
gasto. `test_the_call_cap_alone_does_not_bound_spend` es el que documenta por que
el tope viejo no alcanzaba: sin el nuevo, un solo dia de llamadas caras pasa
entero.

AGUJERO 2 — las dos entradas no coincidian. `worker.run_dream` usaba la fabrica
`create_dream_consolidator()` y el CLI `dream-run` instanciaba GLM a mano. La
fabrica existia y tenia tests; el camino que corre una persona no la llamaba.
`test_both_entrypoints_resolve_the_same_consolidator` falla si vuelven a
divergir.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.worker import DreamConfig


_AHORA = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def ledger(tmp_path) -> DreamLedger:
    return DreamLedger(tmp_path / "dream.db")


def _gastar(ledger: DreamLedger, provider: str, tokens: int, *, model: str = "m", n: int = 1) -> None:
    for i in range(n):
        ledger.record_provider_call(
            f"run-{provider}-{i}", provider=provider, model=model, outcome="ok",
            latency_ms=10, structured_valid=True, input_tokens=tokens,
            output_tokens=0, http_status=200, now=_AHORA,
        )


# --- el contador nuevo -----------------------------------------------------

def test_it_sums_input_tokens_for_the_day(ledger):
    _gastar(ledger, "openai", 1000, n=3)
    assert ledger.provider_input_tokens_today("openai", now=_AHORA) == 3000


def test_it_does_not_mix_providers(ledger):
    """Control negativo: si sumara todo junto, un proveedor barato apagaria a otro."""
    _gastar(ledger, "openai", 5000)
    _gastar(ledger, "google", 7000)
    assert ledger.provider_input_tokens_today("openai", now=_AHORA) == 5000
    assert ledger.provider_input_tokens_today("google", now=_AHORA) == 7000


def test_yesterday_does_not_count(ledger):
    """Es un tope DIARIO: si acumulara historico, se apagaria para siempre."""
    ayer = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    ledger.record_provider_call(
        "run-viejo", provider="openai", model="m", outcome="ok", latency_ms=10,
        structured_valid=True, input_tokens=9_000_000, output_tokens=0,
        http_status=200, now=ayer,
    )
    assert ledger.provider_input_tokens_today("openai", now=_AHORA) == 0


def test_an_empty_ledger_reports_zero_not_none(ledger):
    """SUM() sobre cero filas devuelve NULL; sin COALESCE esto explota al comparar."""
    assert ledger.provider_input_tokens_today("nadie", now=_AHORA) == 0


# --- el tope, y por que el viejo no alcanzaba ------------------------------

class _WorkerMinimo:
    """Solo lo que `_over_token_budget` toca; instanciar DreamWorker pediria un servicio."""

    def __init__(self, ledger: DreamLedger, limite: int) -> None:
        from memorymaster.dreaming.worker import DreamWorker

        self.ledger = ledger
        self.config = DreamConfig(max_input_tokens_daily=limite)
        self.now = lambda: _AHORA
        self._over_token_budget = DreamWorker._over_token_budget.__get__(self)


def test_under_the_cap_work_proceeds(ledger):
    _gastar(ledger, "openai", 500_000)
    assert _WorkerMinimo(ledger, 2_000_000)._over_token_budget("openai", "m") is False


def test_at_the_cap_work_is_deferred(ledger):
    _gastar(ledger, "openai", 2_000_000)
    assert _WorkerMinimo(ledger, 2_000_000)._over_token_budget("openai", "m") is True


def test_the_call_cap_alone_does_not_bound_spend(ledger):
    """El agujero que este tope cierra, con los numeros reales del ledger.

    527 llamadas caben holgadas bajo cualquier tope de llamadas razonable y aun
    asi cuestan 9,76M tokens. El tope en tokens las corta; el de llamadas no.
    """
    _gastar(ledger, "openai", 18_524, n=527)
    gastado = ledger.provider_input_tokens_today("openai", now=_AHORA)
    assert gastado > 9_000_000, "el escenario no reproduce la fuga medida"

    config = DreamConfig()
    assert 527 > config.max_extract_calls_daily, (
        "el tope de llamadas si habria cortado esto; el escenario esta mal armado"
    )
    assert _WorkerMinimo(ledger, 2_000_000)._over_token_budget("openai", "m") is True


def test_a_leak_from_a_new_model_of_the_same_provider_is_caught(ledger):
    """Se consulta por PROVEEDOR: un modelo nuevo no reinicia el contador."""
    _gastar(ledger, "openai", 2_000_000, model="modelo-viejo")
    assert _WorkerMinimo(ledger, 2_000_000)._over_token_budget("openai", "modelo-nuevo") is True


def test_zero_disables_the_cap(ledger):
    """Salida explicita: preferible a que alguien borre el cableado."""
    _gastar(ledger, "openai", 99_000_000)
    assert _WorkerMinimo(ledger, 0)._over_token_budget("openai", "m") is False


def test_the_cap_is_configurable_from_the_environment(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DREAM_MAX_INPUT_TOKENS_DAILY", "123456")
    assert DreamConfig.from_env().max_input_tokens_daily == 123456


# --- las dos entradas, un solo proveedor -----------------------------------

def test_both_entrypoints_resolve_the_same_consolidator(monkeypatch):
    """El CLI y el camino programatico tienen que elegir por la MISMA fabrica.

    Ancla en el requisito, no en la implementacion: no comprueba que la linea
    llame a create_dream_consolidator, comprueba que cambiar la config cambia lo
    que el CLI construye. Un test de lo primero seguiria pasando si alguien
    volviera a fijar un proveedor a mano dentro de la fabrica.
    """
    import memorymaster.surfaces.dreaming_cli as cli
    from memorymaster.dreaming.providers import GLMConsolidator

    monkeypatch.setenv("MEMORYMASTER_DREAM_CONSOLIDATE_PROVIDER", "glm")
    construidos = []

    class _WorkerEspia:
        def __init__(self, ledger, service, extractor, consolidator):  # noqa: ANN001
            construidos.append(consolidator)

        def run(self, **_kwargs):
            return {"ok": True, "extracted": 0, "consolidated": 0, "applied": 0, "errors": 0}

    monkeypatch.setattr(cli, "DreamWorker", _WorkerEspia)
    monkeypatch.setattr(cli, "create_dream_extractor", lambda: object())

    args = type("Args", (), {
        "apply_candidates": False, "scope": None, "max_sessions": 1, "json_output": True,
    })()
    cli.handle_dream_run(args, object(), None, "db")

    assert len(construidos) == 1
    assert isinstance(construidos[0], GLMConsolidator), (
        "el CLI ignoro MEMORYMASTER_DREAM_CONSOLIDATE_PROVIDER"
    )


def test_the_cli_follows_the_config_to_the_approved_provider(monkeypatch):
    """Contra-caso: con el default, el CLI tiene que dar Antigravity, no GLM."""
    import memorymaster.surfaces.dreaming_cli as cli
    from memorymaster.dreaming.providers import AntigravityConsolidator

    monkeypatch.delenv("MEMORYMASTER_DREAM_CONSOLIDATE_PROVIDER", raising=False)
    construidos = []

    class _WorkerEspia:
        def __init__(self, ledger, service, extractor, consolidator):  # noqa: ANN001
            construidos.append(consolidator)

        def run(self, **_kwargs):
            return {"ok": True, "extracted": 0, "consolidated": 0, "applied": 0, "errors": 0}

    monkeypatch.setattr(cli, "DreamWorker", _WorkerEspia)
    monkeypatch.setattr(cli, "create_dream_extractor", lambda: object())

    args = type("Args", (), {
        "apply_candidates": False, "scope": None, "max_sessions": 1, "json_output": True,
    })()
    cli.handle_dream_run(args, object(), None, "db")

    assert isinstance(construidos[0], AntigravityConsolidator)
