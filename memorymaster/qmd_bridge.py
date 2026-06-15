"""Deprecated compatibility shim — moved to ``memorymaster.bridges.qmd_bridge``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.bridges.qmd_bridge``.
"""
import sys as _sys

from memorymaster.bridges import qmd_bridge as _new

_sys.modules[__name__] = _new
