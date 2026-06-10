"""Deprecated compatibility shim — moved to ``memorymaster.surfaces.cli_handlers_integrity``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.surfaces.cli_handlers_integrity``.
"""
import sys as _sys

from memorymaster.surfaces import cli_handlers_integrity as _new

_sys.modules[__name__] = _new
