"""Deprecated compatibility shim — moved to ``memorymaster.stores.store_factory``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.stores.store_factory``.
"""
import sys as _sys

from memorymaster.stores import store_factory as _new

_sys.modules[__name__] = _new
