"""Deprecated compatibility shim — moved to ``memorymaster.bridges.db_merge``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.bridges.db_merge``.
"""
import sys as _sys

from memorymaster.bridges import db_merge as _new

_sys.modules[__name__] = _new
