"""Un pane anidado tiene que poder LEER el scope al que se le manda ESCRIBIR.

EL BUG QUE ORIGINA ESTE ARCHIVO. El allowlist de lectura se derivaba del
directorio del workspace y nada mas, asi que una sesion en `Py Apps/infra` veia
`project:infra` y `global`. Pero el CLAUDE.md raiz de ese arbol le ordena a toda
sesion bajo el ingestar en `project:py-apps` — precisamente para evitar la
fragmentacion. La escritura obedecia la instruccion y la lectura no la conocia.

Medido el 2026-08-21: 9 de 10 panes de la flota no podian leer el scope al que
se les mandaba escribir, sobre ~3000 claims vivas. Y no habia sintoma: el recall
devolvia las claims del scope propio y se leia como si funcionara. Cada sesion
que cumplio bien la regla se escribio a si misma fuera de su propio alcance.

Habia un segundo agujero de la misma familia: `canonicalize_slug` pliega los
sufijos de canal, asi que el workspace `whatsappbot-final` resuelve a
`project:whatsappbot`, mientras que un ingest con scope EXPLICITO guarda el
string crudo. 3901 claims vivas quedaron en `project:whatsappbot-final`, un
scope al que ningun workspace podia resolver jamas.

EL ARREGLO ES DE LECTURA, NO DE DATOS. No se reescribe ninguna fila: reescribir
el scope de ~10.000 claims es irreversible y podria mezclar proyectos que hoy
estan aislados. Se ensancha lo que una consulta ALCANZA, y solo en el camino
derivado — el guard de modo TEAM nunca llega ahi, cosa que el ultimo test de
este archivo verifica explicitamente.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core.scope_utils import (
    ancestor_project_scopes,
    project_scope_variants,
)


def _tree(tmp_path: Path, *, marker: str = "CLAUDE.md") -> Path:
    """Un arbol tipo Py Apps: raiz con marcador, subproyecto adentro."""
    root = tmp_path / "Py Apps"
    (root / "infra").mkdir(parents=True)
    (root / marker).write_text("instrucciones de la raiz", encoding="utf-8")
    return root


# --- el ascenso ------------------------------------------------------------

def test_a_nested_workspace_reaches_its_enclosing_project(tmp_path: Path):
    root = _tree(tmp_path)
    assert ancestor_project_scopes(root / "infra") == ["project:py-apps"]


def test_the_walk_stops_where_the_markers_stop(tmp_path: Path):
    """EL CONTROL QUE IMPIDE QUE ESTO SE DESBOQUE.

    Sin el, cualquier directorio del camino se volveria un scope y una consulta
    terminaria alcanzando media maquina. `Desktop` no lleva marcador y por eso
    no es un proyecto.
    """
    root = _tree(tmp_path)
    scopes = ancestor_project_scopes(root / "infra")
    assert scopes == ["project:py-apps"], (
        f"se alcanzaron {scopes}; solo el ancestro CON marcador es un proyecto"
    )
    for scope in scopes:
        assert tmp_path.name.lower() not in scope


def test_a_workspace_with_no_project_ancestor_reaches_nothing(tmp_path: Path):
    """Contra-caso: sin marcadores arriba, el ascenso devuelve vacio."""
    solo = tmp_path / "suelto" / "adentro"
    solo.mkdir(parents=True)
    assert ancestor_project_scopes(solo) == []


def test_agents_md_also_marks_a_project_root(tmp_path: Path):
    root = _tree(tmp_path, marker="AGENTS.md")
    assert ancestor_project_scopes(root / "infra") == ["project:py-apps"]


def test_the_workspace_itself_is_never_in_its_ancestors(tmp_path: Path):
    root = _tree(tmp_path)
    (root / "infra" / "CLAUDE.md").write_text("propias", encoding="utf-8")
    assert "project:infra" not in ancestor_project_scopes(root / "infra")


def test_a_missing_path_returns_empty_instead_of_raising(tmp_path: Path):
    """Esto alimenta un camino de LECTURA: una consulta no puede romperse por un scope."""
    assert ancestor_project_scopes(tmp_path / "no" / "existe") == []
    assert ancestor_project_scopes("") == []
    assert ancestor_project_scopes(None) == []


def test_the_walk_is_depth_bounded(tmp_path: Path):
    hondo = tmp_path
    for nivel in range(8):
        hondo = hondo / f"n{nivel}"
    hondo.mkdir(parents=True)
    for parent in list(hondo.parents)[:8]:
        (parent / "CLAUDE.md").write_text("x", encoding="utf-8")
    assert len(ancestor_project_scopes(hondo, max_depth=4)) <= 4


# --- las dos grafias del scope propio --------------------------------------

def test_a_channel_suffixed_workspace_reaches_both_spellings():
    """3901 claims vivas dependian de esto."""
    variantes = project_scope_variants("whatsappbot-final")
    assert variantes == ["project:whatsappbot", "project:whatsappbot-final"]


def test_a_plain_workspace_yields_exactly_one_scope():
    """Contra-caso: sin sufijo de canal no se inventa una segunda grafia."""
    assert project_scope_variants("memorymaster") == ["project:memorymaster"]


@pytest.mark.parametrize("dirname,esperados", [
    ("api-prod", ["project:api", "project:api-prod"]),
    ("app-staging", ["project:app", "project:app-staging"]),
    ("infra", ["project:infra"]),
])
def test_scope_variants(dirname, esperados):
    assert project_scope_variants(dirname) == esperados


# --- el allowlist completo, y el limite que NO se movio ---------------------

def test_the_derived_allowlist_reaches_ancestor_and_user(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MEMORYMASTER_MCP_AUTH_MODE", raising=False)
    monkeypatch.delenv("MEMORYMASTER_DEFAULT_PROJECT_SCOPE", raising=False)
    from memorymaster.surfaces import mcp_server as m

    root = _tree(tmp_path)
    allow = m._effective_scope_allowlist("", str(root / "infra"))

    assert "project:infra" in allow, "se perdio el scope propio del workspace"
    assert "project:py-apps" in allow, "no alcanza el proyecto que lo contiene"
    assert "user" in allow, "no alcanza el scope user, donde vive lo transversal"


def test_an_explicit_allowlist_still_wins(tmp_path: Path, monkeypatch):
    """Pedir scopes explicitos no debe traer los ancestros de arriba."""
    monkeypatch.delenv("MEMORYMASTER_MCP_AUTH_MODE", raising=False)
    from memorymaster.surfaces import mcp_server as m

    allow = m._effective_scope_allowlist("project:solo-este", str(_tree(tmp_path) / "infra"))
    assert allow == ["project:solo-este"]


def test_the_team_grant_is_not_widened(tmp_path: Path, monkeypatch):
    """EL CONTROL DE SEGURIDAD. Ensanchar lecturas no puede ensanchar un permiso.

    En modo TEAM el allowlist sale del grant autenticado y nunca del disco. Si
    este test se cae, el arreglo de alcance se convirtio en una fuga entre
    inquilinos.
    """
    from memorymaster.core.access_control import AuthMode, RequestContext, Role
    from memorymaster.surfaces import mcp_server as m

    contexto = RequestContext(
        mode=AuthMode.TEAM,
        principal="p",
        role=Role.READER,
        tenant_id="t",
        workspace=str(tmp_path),
        allowed_scopes=frozenset({"project:solo-permitido"}),
        allow_sensitive=False,
        db_target="x.db",
    )
    monkeypatch.setattr(m, "current_request_context", lambda: contexto)

    allow = m._effective_scope_allowlist("", str(_tree(tmp_path) / "infra"))
    assert allow == ["project:solo-permitido"], (
        f"el allowlist de TEAM devolvio {allow}: el ensanchado de lectura se "
        "filtro al grant autenticado"
    )
    for prohibido in ("project:py-apps", "user", "global"):
        assert prohibido not in allow


def test_the_widening_can_be_turned_off(tmp_path: Path, monkeypatch):
    """El comportamiento previo sigue disponible con una variable de entorno."""
    monkeypatch.delenv("MEMORYMASTER_MCP_AUTH_MODE", raising=False)
    monkeypatch.setenv("MEMORYMASTER_RECALL_ANCESTOR_SCOPES", "0")
    import importlib

    from memorymaster.surfaces import mcp_server as m

    importlib.reload(m)
    try:
        allow = m._effective_scope_allowlist("", str(_tree(tmp_path) / "infra"))
        assert "project:py-apps" not in allow
        assert "user" not in allow
        assert "project:infra" in allow
    finally:
        monkeypatch.delenv("MEMORYMASTER_RECALL_ANCESTOR_SCOPES", raising=False)
        importlib.reload(m)
