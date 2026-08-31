"""La espera de arranque del servidor MCP tiene que esperar de verdad.

El bucle original dormia SOLO en el `except`. Si el servidor aceptaba la
conexion pero devolvia un estado distinto de 200 —el caso normal mientras
arranca— giraba sus 100 vueltas en milisegundos y se rendia sin haber esperado
nada. Y con un limite en vueltas y no en tiempo, el presupuesto real dependia de
si cada intento fallaba rapido o agotaba su timeout: no habia presupuesto.

El sintoma no era un error de arranque legible. El test avanzaba, la llamada MCP
fallaba, y `_classify_transport_error` la reportaba como `authority_unavailable`
— su fallback para cualquier error de transporte sin clasificar. Cuatro caidas
en CI el 2026-08-31, todas en Windows, entre 1 y 1,5 horas de suite cada una.

Estos tests anclan el REQUISITO ("la espera consume su presupuesto de tiempo
antes de rendirse"), no la implementacion. Y fijan explicitamente que esto NO es
un reintento: no repite aserciones ni tolera un fallo persistente.
"""
from __future__ import annotations

import time

import httpx

from test_hermes_memory_provider_http import _wait_until_healthy


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def test_duerme_tambien_cuando_responde_distinto_de_200(monkeypatch):
    """El bug exacto: sin sleep en esta rama, el presupuesto se evapora."""
    llamadas = {"get": 0, "sleep": 0.0}

    def fake_get(url, timeout=None):
        llamadas["get"] += 1
        return _Resp(503)

    def fake_sleep(seconds):
        llamadas["sleep"] += seconds

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(time, "sleep", fake_sleep)

    assert _wait_until_healthy("http://x/healthz", budget_seconds=0.3) is False
    assert llamadas["get"] > 0
    assert llamadas["sleep"] > 0, (
        "giro sin dormir ante un 503: es el defecto que dejaba el arranque sin espera"
    )


def test_devuelve_true_apenas_hay_200(monkeypatch):
    estados = [503, 503, 200]

    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: _Resp(estados.pop(0)))
    monkeypatch.setattr(time, "sleep", lambda s: None)

    assert _wait_until_healthy("http://x/healthz", budget_seconds=5.0) is True
    assert not estados, "no consumio los intentos previos"


def test_tolera_errores_de_transporte_mientras_arranca(monkeypatch):
    intentos = {"n": 0}

    def fake_get(url, timeout=None):
        intentos["n"] += 1
        if intentos["n"] < 3:
            raise httpx.ConnectError("connection refused")
        return _Resp(200)

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    assert _wait_until_healthy("http://x/healthz", budget_seconds=5.0) is True


def test_el_limite_es_TIEMPO_y_se_respeta(monkeypatch):
    """Sin limite temporal no hay presupuesto: 100 vueltas duran lo que duren."""
    reloj = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: reloj["t"])
    monkeypatch.setattr(time, "sleep", lambda s: reloj.__setitem__("t", reloj["t"] + s))
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: _Resp(500))

    inicio = reloj["t"]
    assert _wait_until_healthy("http://x/h", budget_seconds=2.0, poll_seconds=0.1) is False
    consumido = reloj["t"] - inicio
    assert 2.0 <= consumido < 2.5, f"no consumio su presupuesto: {consumido}"


def test_no_es_un_reintento_del_test(monkeypatch):
    """Un servidor que NUNCA levanta sigue fallando: no tapa una caida real."""
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: (_ for _ in ()).throw(
        httpx.ConnectError("nunca levanta")))
    monkeypatch.setattr(time, "sleep", lambda s: None)

    assert _wait_until_healthy("http://x/h", budget_seconds=0.2) is False
