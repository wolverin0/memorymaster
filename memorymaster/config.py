"""Deprecated compatibility shim — moved to ``memorymaster.core.config``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.core.config``.
"""
import sys as _sys

from memorymaster.core import config as _new

_sys.modules[__name__] = _new
