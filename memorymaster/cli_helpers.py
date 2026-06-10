"""Deprecated compatibility shim — moved to ``memorymaster.surfaces.cli_helpers``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.surfaces.cli_helpers``.
"""
import sys as _sys

from memorymaster.surfaces import cli_helpers as _new

_sys.modules[__name__] = _new
