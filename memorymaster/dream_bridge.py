"""Deprecated compatibility shim — moved to ``memorymaster.bridges.dream_bridge``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.bridges.dream_bridge``.
"""
import sys as _sys

from memorymaster.bridges import dream_bridge as _new

_sys.modules[__name__] = _new
