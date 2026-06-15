"""Deprecated compatibility shim — moved to ``memorymaster.recall.query_cache``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.recall.query_cache``.
"""
import sys as _sys

from memorymaster.recall import query_cache as _new

_sys.modules[__name__] = _new
