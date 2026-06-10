"""Deprecated compatibility shim — moved to ``memorymaster.govern.contradiction_probe``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.govern.contradiction_probe``.
"""
import sys as _sys

from memorymaster.govern import contradiction_probe as _new

_sys.modules[__name__] = _new
