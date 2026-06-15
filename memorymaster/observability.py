"""Deprecated compatibility shim — moved to ``memorymaster.core.observability``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.core.observability``.
"""
import sys as _sys

from memorymaster.core import observability as _new

_sys.modules[__name__] = _new
