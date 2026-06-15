"""Deprecated compatibility shim — moved to ``memorymaster.surfaces.operator_queue``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.surfaces.operator_queue``.
"""
import sys as _sys

from memorymaster.surfaces import operator_queue as _new

_sys.modules[__name__] = _new
