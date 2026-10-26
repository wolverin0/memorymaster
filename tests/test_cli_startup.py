"""CLI help must not load hook execution or installation machinery."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize("arguments", [[], ["run-steward"], ["hooks", "doctor"]])
def test_cli_help_does_not_import_hook_execution(arguments):
    script = """
import importlib.abc
import runpy
import sys

class BlockHookExecution(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {
            "memorymaster.surfaces.hooks_doctor",
            "memorymaster.surfaces.hook_probe",
            "memorymaster.surfaces.setup_hooks",
        }:
            raise AssertionError("eager hook import: " + fullname)

sys.meta_path.insert(0, BlockHookExecution())
runpy.run_module("memorymaster", run_name="__main__")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, *arguments, "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
