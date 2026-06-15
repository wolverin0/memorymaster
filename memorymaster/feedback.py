"""Deprecated compatibility shim — moved to ``memorymaster.govern.feedback``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.govern.feedback``.
"""
import sys as _sys

from memorymaster.govern import feedback as _new

_sys.modules[__name__] = _new
