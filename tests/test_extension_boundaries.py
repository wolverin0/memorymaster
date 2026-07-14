"""R4.1 acceptance pins for core and optional companion boundaries."""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "memorymaster"
CORE_TREES = ("core", "govern", "recall", "stores")
OPTIONAL_PREFIXES = (
    "memorymaster.bridges",
    "memorymaster.knowledge.wiki",
    "memorymaster.knowledge.vault",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_core_layers_do_not_import_optional_companions() -> None:
    offenders: list[str] = []
    for tree_name in CORE_TREES:
        for path in (PACKAGE / tree_name).rglob("*.py"):
            for imported in _imports(path):
                if imported.startswith(OPTIONAL_PREFIXES):
                    offenders.append(f"{path.relative_to(REPO)} -> {imported}")
    assert offenders == []


def test_core_service_import_does_not_load_or_register_companions() -> None:
    code = r'''
        import importlib.abc
        import sys

        blocked = (
            "memorymaster.bridges",
            "memorymaster.knowledge.wiki",
            "memorymaster.knowledge.vault",
        )

        class Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.startswith(blocked):
                    raise ImportError(f"optional companion loaded by core: {fullname}")
                return None

        sys.meta_path.insert(0, Blocker())
        import memorymaster.core.service  # noqa: F401
        from memorymaster.core import lifecycle

        assert lifecycle.on_claim_confirmed is None
        print("OK")
    '''
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr


def test_explicit_wiki_import_registers_its_lifecycle_adapter() -> None:
    code = r'''
        from memorymaster.core import lifecycle
        assert lifecycle.on_claim_confirmed is None

        import memorymaster.knowledge.wiki_engine  # noqa: F401
        assert lifecycle.on_claim_confirmed is not None
        print("OK")
    '''
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr


def test_dead_generic_plugin_surface_is_removed() -> None:
    assert not (PACKAGE / "core" / "plugins.py").exists()
    assert not (PACKAGE / "plugins.py").exists()
    assert "memorymaster.plugins" not in (REPO / "pyproject.toml").read_text(encoding="utf-8")
