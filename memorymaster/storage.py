"""Deprecated compatibility shim — moved to ``memorymaster.stores.storage``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.stores.storage``.
"""
import sys as _sys

from memorymaster.stores import storage as _new

_sys.modules[__name__] = _new
