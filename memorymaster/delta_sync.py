"""Deprecated compatibility shim — moved to ``memorymaster.bridges.delta_sync``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.bridges.delta_sync``.
"""
import sys as _sys

from memorymaster.bridges import delta_sync as _new

_sys.modules[__name__] = _new
