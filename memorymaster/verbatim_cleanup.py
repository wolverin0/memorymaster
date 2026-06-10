"""Deprecated compatibility shim — moved to ``memorymaster.govern.verbatim_cleanup``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.govern.verbatim_cleanup``.
"""
import sys as _sys

from memorymaster.govern import verbatim_cleanup as _new

_sys.modules[__name__] = _new
