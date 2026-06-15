"""Deprecated compatibility shim — moved to ``memorymaster.core.access_control``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.core.access_control``.
"""
import sys as _sys

from memorymaster.core import access_control as _new

_sys.modules[__name__] = _new
