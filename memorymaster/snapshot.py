"""Deprecated compatibility shim — moved to ``memorymaster.stores.snapshot``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.stores.snapshot``.
"""
import sys as _sys

from memorymaster.stores import snapshot as _new

_sys.modules[__name__] = _new
