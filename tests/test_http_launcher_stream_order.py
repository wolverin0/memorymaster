"""Consoleless launchers establish streams before importing logging users."""

import builtins
import io
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_pythonw_streams_exist_when_http_server_is_imported(monkeypatch):
    launcher = (
        Path(__file__).parents[1] / "integrations" / "hermes-memorymaster"
        / "templates" / "windows" / "memorymaster-mcp-http.pyw"
    )
    stream = io.StringIO()
    original_import = builtins.__import__
    observations = []

    def import_checked(name, *args, **kwargs):
        if name == "memorymaster.surfaces.mcp_http":
            observations.append((sys.stdout is stream, sys.stderr is stream))
            return SimpleNamespace(main=lambda: 0)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_checked)
    monkeypatch.setattr(Path, "mkdir", lambda *args, **kwargs: None)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: stream)
    monkeypatch.setenv("LOCALAPPDATA", "fixture")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(launcher), run_name="__main__")
    assert exit_info.value.code == 0
    assert observations == [(True, True)]
