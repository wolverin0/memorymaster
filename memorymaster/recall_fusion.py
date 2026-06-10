"""Deprecated compatibility shim — moved to ``memorymaster.recall.recall_fusion``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.recall.recall_fusion``.
"""
import sys as _sys

from memorymaster.recall import recall_fusion as _new

_sys.modules[__name__] = _new
