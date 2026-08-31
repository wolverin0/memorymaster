"""Tests del guard que exige guardar la causa, no solo la etiqueta.

Un guard sin tests es una superstición: no se sabe si atrapa lo que dice ni,
peor, si dispara sobre codigo sano. Lo segundo importa mas — un check que ladra
ante codigo que ya cumple entrena a apaciguarlo, y termina desactivado.

Por eso hay tantos casos de SILENCIO como de alarma, y el ultimo test corre el
guard contra el repo real: la unica prueba de que es sostenible es que main pase.
"""
from __future__ import annotations

from pathlib import Path

from scripts.check_swallowed_cause import check_paths, check_source

REPO = Path(__file__).resolve().parents[1]


def _n(src: str) -> int:
    return len(check_source(src, "x.py"))


# --- lo que DEBE marcar -----------------------------------------------------

def test_marca_except_sin_ligar_que_escribe_codigo_constante():
    """El caso exacto que costo diez dias en graph_observation_engine."""
    assert _n(
        "try:\n    f()\n"
        "except Exception:\n"
        "    repo.fail_job(1, error_code='synthesis_failed')\n"
    ) == 1


def test_marca_aunque_ligue_si_nunca_usa_la_excepcion():
    """Poner `as exc` y no usarlo es la misma perdida con mejor apariencia."""
    assert _n(
        "try:\n    f()\n"
        "except Exception as exc:\n"
        "    repo.fail_job(1, error_code='synthesis_failed')\n"
    ) == 1


def test_marca_cada_palabra_de_causa():
    for palabra in ("error_code", "error", "reason", "outcome"):
        src = (
            "try:\n    f()\n"
            "except Exception:\n"
            f"    ledger.finish(1, {palabra}='error')\n"
        )
        assert _n(src) == 1, f"no marco {palabra}"


# --- lo que debe CALLAR (lo que hace sostenible al guard) -------------------

def test_calla_si_liga_y_usa_la_excepcion():
    """Etiqueta + detalle es correcto: la etiqueta clasifica, el detalle explica."""
    assert _n(
        "try:\n    f()\n"
        "except Exception as exc:\n"
        "    repo.fail_job(1, error_code='synthesis_failed',"
        " detail=f'{type(exc).__name__}: {exc}')\n"
    ) == 0


def test_calla_si_solo_loguea_la_excepcion():
    assert _n(
        "try:\n    f()\n"
        "except Exception as exc:\n"
        "    logger.warning('fallo: %s', exc)\n"
        "    repo.fail_job(1, error_code='synthesis_failed')\n"
    ) == 0


def test_calla_si_re_lanza():
    """Propagar preserva la causa por definicion."""
    assert _n(
        "try:\n    f()\n"
        "except Exception:\n"
        "    repo.fail_job(1, error_code='x')\n"
        "    raise\n"
    ) == 0


def test_calla_ante_el_escape_declarado():
    assert _n(
        "try:\n    f()\n"
        "except Exception:  # swallow-ok: hook quiet-by-contract\n"
        "    ledger.finish(1, outcome='error')\n"
    ) == 0


def test_calla_si_el_codigo_no_es_constante():
    """Un valor NO literal no es una etiqueta fija: lleva algo adentro.

    Sin ligar la excepcion A PROPOSITO. La version anterior de este test hacia
    `except Exception as exc` y usaba `exc` dentro del f-string, asi que salia
    exento por la regla de "liga y usa" y nunca llegaba al chequeo de constante:
    pasaba por el motivo equivocado. Lo encontro una prueba de mutacion — al
    sacar el `isinstance(..., ast.Constant)` este test seguia verde y la
    mutacion solo moria por un AttributeError incidental en otro test.
    """
    assert _n(
        "try:\n    f()\n"
        "except Exception:\n"
        "    repo.fail_job(1, error_code=codigo_calculado)\n"
    ) == 0


def test_calla_ante_un_codigo_construido_en_f_string():
    """Mismo requisito, otra forma sintactica: JoinedStr en vez de Name."""
    assert _n(
        "try:\n    f()\n"
        "except Exception:\n"
        "    repo.fail_job(1, error_code=f'failed:{contexto}')\n"
    ) == 0


def test_calla_ante_un_except_que_no_escribe_causa():
    """La mayoria de los handlers del repo: no tocan un campo de causa."""
    assert _n(
        "try:\n    f()\n"
        "except Exception:\n"
        "    data = {}\n"
    ) == 0


def test_calla_ante_palabras_parecidas_pero_no_de_causa():
    """`status` y `code` se usan para mil cosas; marcarlos inundaria de falsos."""
    assert _n(
        "try:\n    f()\n"
        "except Exception:\n"
        "    resp(status='error', code='500')\n"
    ) == 0


# --- el repo real -----------------------------------------------------------

# --- los tres casos reales que originaron el guard, como fixtures -----------
# No son ejemplos inventados: son la forma exacta del codigo que costo los dias.
# Si alguien vuelve a escribir cualquiera de estas tres formas, el guard tiene
# que verla — y este test lo prueba sin depender de que el archivo original
# siga existiendo o siga escrito igual.

CASO_1_PERFIL = (
    "try:\n"
    "    self._reduce_and_complete(run_id, now)\n"
    "except AntigravityError:\n"
    "    self.repo.fail_run(run_id, error_code='AntigravityError')\n"
)

CASO_2_SINTESIS = (
    "try:\n"
    "    raw = self.llm_call(SYSTEM_PROMPT, prompt)\n"
    "except Exception:  # noqa: BLE001 - fail closed and retry from IDs\n"
    "    self.repo.fail_job(job.id, owner=owner, error_code='synthesis_failed')\n"
)

CASO_3_DISCOVERY = (
    "try:\n"
    "    self.repo.complete_job(job.id, owner=owner, outcome=outcome)\n"
    "except Exception:  # noqa: BLE001 - typed retry boundary persisted below\n"
    "    self.repo.fail_job(job.id, owner=owner, error_code='discovery_failed')\n"
)


def test_atrapa_los_tres_casos_reales_que_lo_originaron():
    for nombre, fuente in (
        ("perfil compilado (10 dias de diagnostico equivocado)", CASO_1_PERFIL),
        ("sintesis PPR-7 (5 intentos, misma palabra)", CASO_2_SINTESIS),
        ("discovery PPR-7 (hermana que nadie habia visto)", CASO_3_DISCOVERY),
    ):
        assert _n(fuente) == 1, f"el guard NO ve el caso real: {nombre}"


def test_los_tres_casos_reales_arreglados_ya_no_disparan():
    """La otra mitad: el arreglo que se aplico tiene que dejar de marcar.

    Un guard que sigue ladrando despues del fix no distingue el problema de la
    solucion, y lo unico que ensena es a silenciarlo.
    """
    arreglado = (
        "try:\n"
        "    raw = self.llm_call(SYSTEM_PROMPT, prompt)\n"
        "except Exception as exc:  # noqa: BLE001\n"
        "    self.repo.fail_job(job.id, owner=owner, error_code='synthesis_failed',\n"
        "                       detail=f'{type(exc).__name__}: {exc}')\n"
    )
    assert _n(arreglado) == 0


def test_el_repo_pasa_el_guard():
    """Si esto se pone rojo, o hay un handler nuevo que tira la causa o el guard
    se volvio demasiado ancho. Las dos merecen mirarse, ninguna silenciarse."""
    violations = check_paths([REPO / "memorymaster"])
    assert not violations, "\n".join(str(v) for v in violations)
