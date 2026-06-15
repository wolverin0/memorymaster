"""Deprecated compatibility shim — moved to ``memorymaster.stores._storage_lifecycle``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.stores._storage_lifecycle``.
"""
import sys as _sys

from memorymaster.stores import _storage_lifecycle as _new

_sys.modules[__name__] = _new
