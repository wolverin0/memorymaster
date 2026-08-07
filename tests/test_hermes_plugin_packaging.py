from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


PLUGIN_SRC = Path(__file__).parents[1] / "integrations" / "hermes-memorymaster" / "src"
if str(PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(PLUGIN_SRC))

from hermes_memorymaster.installer import install_plugin  # noqa: E402
from hermes_memorymaster.provider import MemoryMasterProvider  # noqa: E402


def test_plugin_install_is_previewed_then_discoverable_from_hermes_home(
    tmp_path: Path,
) -> None:
    preview = install_plugin(tmp_path, apply=False)
    target = tmp_path / "plugins" / "memorymaster"
    assert preview["target"] == str(target.resolve())
    assert preview["apply"] is False
    assert not target.exists()

    applied = install_plugin(tmp_path, apply=True)

    assert applied["written"] == ["__init__.py", "cli.py", "plugin.yaml"]
    spec = importlib.util.spec_from_file_location(
        "hermes_fixture_memorymaster",
        target / "__init__.py",
        submodule_search_locations=[str(target)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Collector:
        provider = None

        def register_memory_provider(self, provider) -> None:
            self.provider = provider

    collector = Collector()
    module.register(collector)
    assert isinstance(collector.provider, MemoryMasterProvider)


def test_plugin_install_refuses_to_overwrite_unowned_changes(tmp_path: Path) -> None:
    install_plugin(tmp_path, apply=True)
    target = tmp_path / "plugins" / "memorymaster" / "plugin.yaml"
    target.write_text("operator-owned change", encoding="utf-8")

    with pytest.raises(FileExistsError, match="--force"):
        install_plugin(tmp_path, apply=True)

    assert target.read_text(encoding="utf-8") == "operator-owned change"
