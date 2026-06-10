"""Deprecated compatibility shim — moved to ``memorymaster.recall.verbatim_store``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.recall.verbatim_store``.
"""
import sys as _sys

from memorymaster.recall import verbatim_store as _new

_sys.modules[__name__] = _new
