"""Deprecated compatibility shim — moved to ``memorymaster.recall.qdrant_recall_fallback``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.recall.qdrant_recall_fallback``.
"""
import sys as _sys

from memorymaster.recall import qdrant_recall_fallback as _new

_sys.modules[__name__] = _new
