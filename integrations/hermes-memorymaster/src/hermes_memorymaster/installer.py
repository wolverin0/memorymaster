"""Previewed installer for the pinned Hermes user-memory-provider layout."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Any


PLUGIN_FILES = ("__init__.py", "cli.py", "plugin.yaml")


def _plugin_content(name: str) -> str:
    resource = files("hermes_memorymaster.plugin_files").joinpath(name)
    return resource.read_text(encoding="utf-8")


def install_plugin(
    hermes_home: str | Path,
    *,
    apply: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Preview or install the provider shim without editing Hermes core/config."""
    home = Path(hermes_home).expanduser().resolve()
    target = (home / "plugins" / "memorymaster").resolve()
    if home not in target.parents:
        raise ValueError("Hermes plugin target escaped HERMES_HOME")
    states: dict[str, str] = {}
    content = {name: _plugin_content(name) for name in PLUGIN_FILES}
    for name, expected in content.items():
        destination = target / name
        if not destination.exists():
            states[name] = "create"
        elif destination.read_text(encoding="utf-8") == expected:
            states[name] = "unchanged"
        else:
            states[name] = "replace"
    if apply and "replace" in states.values() and not force:
        raise FileExistsError("Existing Hermes plugin differs; preview then repeat with --force")
    written: list[str] = []
    if apply:
        target.mkdir(parents=True, exist_ok=True)
        for name, expected in content.items():
            if states[name] != "unchanged":
                (target / name).write_text(expected, encoding="utf-8")
                written.append(name)
    return {
        "apply": apply,
        "target": str(target),
        "files": states,
        "written": written,
        "config_changed": False,
    }
