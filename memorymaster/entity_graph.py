"""Deprecated compatibility shim — moved to ``memorymaster.knowledge.entity_graph``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.knowledge.entity_graph``.
"""
import sys as _sys

from memorymaster.knowledge import entity_graph as _new

_sys.modules[__name__] = _new
