"""Deprecated compatibility shim — moved to ``memorymaster.surfaces.operator``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.surfaces.operator``.
"""
import sys as _sys

from memorymaster.surfaces import operator as _new

_sys.modules[__name__] = _new
