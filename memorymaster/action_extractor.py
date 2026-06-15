"""Deprecated compatibility shim — moved to ``memorymaster.bridges.action_extractor``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.bridges.action_extractor``.
"""
import sys as _sys

from memorymaster.bridges import action_extractor as _new

_sys.modules[__name__] = _new
