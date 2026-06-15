"""Deprecated compatibility shim — moved to ``memorymaster.bridges.atlas_contract``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.bridges.atlas_contract``.
"""
import sys as _sys

from memorymaster.bridges import atlas_contract as _new

_sys.modules[__name__] = _new
