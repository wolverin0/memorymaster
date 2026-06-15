"""Deprecated compatibility shim — moved to ``memorymaster.core.scope_utils``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.core.scope_utils``.
"""
import sys as _sys

from memorymaster.core import scope_utils as _new

_sys.modules[__name__] = _new
