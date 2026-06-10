"""Deprecated compatibility shim — moved to ``memorymaster.recall.context_optimizer``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.recall.context_optimizer``.
"""
import sys as _sys

from memorymaster.recall import context_optimizer as _new

_sys.modules[__name__] = _new
