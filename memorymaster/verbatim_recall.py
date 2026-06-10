"""Deprecated compatibility shim — moved to ``memorymaster.recall.verbatim_recall``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.recall.verbatim_recall``.
"""
import sys as _sys

from memorymaster.recall import verbatim_recall as _new

_sys.modules[__name__] = _new
