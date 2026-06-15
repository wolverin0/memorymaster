"""Deprecated compatibility shim — moved to ``memorymaster.govern.auto_resolver``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.govern.auto_resolver``.
"""
import sys as _sys

from memorymaster.govern import auto_resolver as _new

_sys.modules[__name__] = _new
