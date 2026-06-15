"""Deprecated compatibility shim — moved to ``memorymaster.knowledge.entity_registry``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.knowledge.entity_registry``.
"""
import sys as _sys

from memorymaster.knowledge import entity_registry as _new

_sys.modules[__name__] = _new
