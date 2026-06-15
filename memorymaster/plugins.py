"""Deprecated compatibility shim — moved to ``memorymaster.core.plugins``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.core.plugins``.
"""
import sys as _sys

from memorymaster.core import plugins as _new

_sys.modules[__name__] = _new
