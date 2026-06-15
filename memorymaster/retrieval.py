"""Deprecated compatibility shim — moved to ``memorymaster.recall.retrieval``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.recall.retrieval``.
"""
import sys as _sys

from memorymaster.recall import retrieval as _new

_sys.modules[__name__] = _new
