"""El marcador debe medir ESTE arbol, no el paquete instalado.

Tercera aparicion de la misma trampa el 2026-08-19, y tres veces es patron:

  1. Un egg-info viejo en la raiz hizo que importlib.metadata reportara 3.28.0
     con 4.8.0 instalado, segun desde donde se importara.
  2. Un script de validacion corrido por ruta absoluta importo el paquete
     instalado y casi hizo reportar que un merge habia perdido un arreglo.
  3. El marcador corrido como `python scripts/probe_suite.py` medio site-packages
     durante toda una ronda: los tests de un arreglo pasaban y el marcador seguia
     reportando el defecto, al mismo tiempo.

La tercera es la peor de las tres. Un loop de calidad cuyo instrumento apunta a
otro artefacto no es lento, es ciego: su medicion no puede moverse haga lo que
haga, asi que gasta ciclos arreglando codigo ya correcto y nunca converge.

La causa es de Python y no del proyecto: ejecutar un script pone el directorio
DEL SCRIPT en sys.path, no el directorio de trabajo. Por eso `import memorymaster`
desde scripts/ resuelve a site-packages aunque estes parado en el repo.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
PROBE = REPO / "scripts" / "probe_suite.py"


def _installed_editable() -> bool:
    """True si `pip install -e` hace que memorymaster resuelva SIEMPRE al arbol.

    CI instala editable; esta maquina lo tiene instalado normal. Bajo editable no
    existe ninguna copia en site-packages, asi que los dos casos de abajo que
    EXIGEN site-packages no tienen nada que exigir: su premisa desaparece.

    Se consulta desde un cwd neutro a proposito. Preguntandolo parado en el repo
    la respuesta seria siempre "el arbol" por precedencia de cwd, que es la misma
    confusion que este archivo entero existe para evitar.
    """
    out = subprocess.run(
        [sys.executable, "-c", "import memorymaster; print(memorymaster.__file__)"],
        cwd=tempfile.gettempdir(), capture_output=True, text=True, check=False,
    )
    return out.returncode == 0 and str(REPO) in out.stdout


needs_non_editable = pytest.mark.skipif(
    _installed_editable(),
    reason="instalacion editable: no hay copia en site-packages contra la cual contrastar",
)


def _resolved_package(*extra: str) -> str:
    """Que memorymaster ve el marcador cuando se lo ejecuta como script."""
    # Ejecuta el ENCABEZADO real del marcador (hasta la primera funcion), con
    # __file__ provisto, para que su logica de sys.path corra tal cual.
    code = (
        "import sys;"
        f"sys.argv = ['probe_suite.py', {', '.join(repr(x) for x in extra)}];"
        f"g = {{'__file__': r'{PROBE}', '__name__': 'probe_header'}};"
        f"src = open(r'{PROBE}', encoding='utf-8').read().split('def _service')[0];"
        "exec(compile(src, 'probe_header', 'exec'), g);"
        "import memorymaster; print(memorymaster.__file__)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=PROBE.parent,
        capture_output=True, text=True, check=False,
    )
    return out.stdout.strip().splitlines()[-1] if out.stdout.strip() else out.stderr


def test_probe_imports_the_repository_not_site_packages():
    resolved = pathlib.Path(_resolved_package()).resolve()
    assert str(resolved).startswith(str(REPO)), (
        f"el marcador importo {resolved}\n"
        f"esperado: algo bajo {REPO}\n"
        "Midiendo el paquete instalado, el marcador no puede ver ninguna mejora "
        "de esta rama y el loop nunca converge."
    )
    assert "site-packages" not in str(resolved)


@needs_non_editable
def test_the_installed_escape_hatch_still_works():
    """La salida explicita para cuando SI se quiere medir lo publicado."""
    resolved = _resolved_package("--installed")
    assert "site-packages" in resolved, (
        "--installed debe medir el paquete publicado; si tambien resuelve al "
        "arbol, no queda forma de medir lo que corre en produccion"
    )


@needs_non_editable
def test_the_guard_would_notice_the_bug_it_guards_against():
    """Probar el instrumento: sin la insercion de sys.path, esto debe resolver a site-packages.

    Un guard que nunca vio fallar el caso que vigila no esta probado.

    SKIPEADO BAJO EDITABLE, y hay que decir por que sin adornarlo: bajo editable el
    bug que este guard vigila NO PUEDE OCURRIR — no hay copia en site-packages a la
    cual irse — asi que no queda nada que demostrar. La cobertura que se pierde es
    real y la corrida no-editable de esta maquina es la que la aporta. La escrbi
    asumiendo mi entorno y en CI fallo: el propio archivo que existe para que el
    instrumento no mida otro artefacto, midio el mio.
    """
    code = (
        "import sys; import memorymaster; print(memorymaster.__file__)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=PROBE.parent,
        capture_output=True, text=True, check=False,
    )
    assert "site-packages" in out.stdout, (
        "sin la insercion explicita de la raiz del repo, importar desde scripts/ "
        "deberia resolver al paquete instalado — si no, este guard no esta "
        "vigilando nada y hay que revisar por que"
    )
