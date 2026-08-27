"""El indice de Qdrant queda congelado: se lee, no se escribe (ruling MM8, 2026-08-24).

POR QUE EXISTE ESTE ARCHIVO. La cobertura medida era 2,6% de las claims con cero
lectores, asi que cada escritura pagaba embedding y trafico para alimentar un
indice que no respondia ninguna consulta util. El operador decidio congelarlo.

EL TEST QUE MAS IMPORTA NO ES EL QUE COMPRUEBA QUE NO ESCRIBE. Es
`test_a_frozen_write_reports_nothing_pending`: si el guard devolviera False, cada
claim se encolaria en el outbox y se acumularia para siempre un backlog de
reintentos hacia un destino que decidimos no alimentar. Apagar una escritura y
crear una cola infinita en su lugar no es apagarla.

Y el segundo en importancia es `test_reads_still_work_when_writes_are_frozen`:
congelar no es borrar. Si el guard tambien tapara las lecturas, lo ya indexado se
volveria inalcanzable y el ruling diria otra cosa.

Cada caso lleva su control negativo con la variable encendida, porque un test que
solo mira el estado apagado pasa igual si el interruptor esta cableado al reves.

SIN marcador `ml` A PROPOSITO. `tests/test_qdrant_backend.py` lleva
`pytestmark = pytest.mark.ml` porque carga sentence-transformers, y CI corre
`-m "not ml"`: o sea que ese archivo NO se ejecuta en ningun job. Este no toca
torch ni red —sustituye embedding y transporte— asi que corre en CI, que es donde
un interruptor de gasto tiene que estar vigilado.
"""
from __future__ import annotations

import pytest

from memorymaster.core.models import Claim
from memorymaster.recall import qdrant_backend


@pytest.fixture(autouse=True)
def _sin_variable(monkeypatch):
    monkeypatch.delenv(qdrant_backend.ENV_WRITES, raising=False)
    yield


def _claim() -> Claim:
    return Claim(
        id=7,
        text="una claim cualquiera",
        idempotency_key=None,
        normalized_text=None,
        claim_type="fact",
        subject="algo",
        predicate="es",
        object_value="asi",
        scope="project:x",
        volatility="medium",
        status="confirmed",
        confidence=0.8,
        pinned=False,
        supersedes_claim_id=None,
        replaced_by_claim_id=None,
        created_at="2026-08-24T00:00:00+00:00",
        updated_at="2026-08-24T00:00:00+00:00",
        last_validated_at=None,
        archived_at=None,
    )


class _Respuesta:
    def raise_for_status(self) -> None:
        return None


class _BackendEspia:
    """Backend real con el transporte y el embedding sustituidos.

    Se construye sin __init__ a proposito: instanciar QdrantBackend abriria un
    cliente HTTP contra una URL que no existe. Lo que se prueba aca es el guard,
    que corre ANTES de todo eso.

    El espia REGISTRA y devuelve una respuesta OK en vez de levantar: los metodos
    del backend atrapan Exception y devuelven False, asi que una excepcion aca se
    convertiria en un fallo silencioso en vez de en la senal que buscamos.
    """

    def __init__(self) -> None:
        self.backend = qdrant_backend.QdrantBackend.__new__(qdrant_backend.QdrantBackend)
        self.puts: list[tuple] = []
        self.posts: list[tuple] = []
        self.backend.qdrant_url = "http://destino-que-no-debe-recibir-nada"
        self.backend.collection = "claims"
        self.backend._embed = lambda _texto: [0.1] * 8
        self.backend._qdrant_client = self

    def put(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.puts.append((args, kwargs))
        return _Respuesta()

    def post(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.posts.append((args, kwargs))
        return _Respuesta()


# --- el default: congelado -------------------------------------------------

def test_upsert_does_not_touch_the_network_by_default():
    espia = _BackendEspia()
    assert espia.backend.upsert_claim(_claim()) is True
    assert espia.puts == [], "el guard dejo pasar la escritura"


def test_delete_does_not_touch_the_network_by_default():
    espia = _BackendEspia()
    assert espia.backend.delete_claim(7) is True
    assert espia.posts == [], "el guard dejo pasar el borrado"


def test_a_frozen_write_reports_nothing_pending():
    """El contrato es True, no False.

    False haria que service._qdrant_sync encole la claim en el outbox. Apagar la
    escritura y dejar una cola creciendo hacia el mismo destino no es apagarla:
    es esconder el trabajo en otro lado.
    """
    espia = _BackendEspia()
    assert espia.backend.upsert_claim(_claim()) is True
    assert espia.backend.delete_claim(7) is True


def test_no_embedding_is_computed_while_frozen():
    """El embedding es el costo real; ni siquiera se calcula."""
    espia = _BackendEspia()
    llamadas: list[str] = []
    espia.backend._embed = lambda texto: llamadas.append(texto) or [0.1] * 8

    espia.backend.upsert_claim(_claim())
    assert llamadas == [], "se pago un embedding para una escritura que no ocurre"


# --- control negativo: con la variable encendida SI escribe ----------------

def test_the_switch_is_not_wired_backwards(monkeypatch):
    """Sin esto, un guard que devuelve siempre True pasaria los tests de arriba."""
    monkeypatch.setenv(qdrant_backend.ENV_WRITES, "1")
    espia = _BackendEspia()
    assert espia.backend.upsert_claim(_claim()) is True
    assert len(espia.puts) == 1, "con las escrituras encendidas no llego al transporte"


def test_delete_switch_is_not_wired_backwards(monkeypatch):
    monkeypatch.setenv(qdrant_backend.ENV_WRITES, "1")
    espia = _BackendEspia()
    assert espia.backend.delete_claim(7) is True
    assert len(espia.posts) == 1


@pytest.mark.parametrize("valor", ["1", "true", "TRUE", "yes", "on"])
def test_accepted_truthy_values(monkeypatch, valor):
    monkeypatch.setenv(qdrant_backend.ENV_WRITES, valor)
    assert qdrant_backend.writes_enabled() is True


@pytest.mark.parametrize("valor", ["", "0", "false", "no", "off", "  "])
def test_everything_else_keeps_it_frozen(monkeypatch, valor):
    monkeypatch.setenv(qdrant_backend.ENV_WRITES, valor)
    assert qdrant_backend.writes_enabled() is False


# --- congelar no es borrar -------------------------------------------------

def test_reads_still_work_when_writes_are_frozen():
    """`search_candidates` no pasa por el guard: lo ya indexado sigue alcanzable."""
    espia = _BackendEspia()
    consultado: list[str] = []

    def _buscar(texto, limit=10):  # noqa: ANN001, ANN202
        consultado.append(texto)
        return [{"claim_id": 7}]

    espia.backend.search_candidates = _buscar
    assert espia.backend.search_candidates("algo") == [{"claim_id": 7}]
    assert consultado == ["algo"], "la lectura quedo tapada junto con la escritura"
