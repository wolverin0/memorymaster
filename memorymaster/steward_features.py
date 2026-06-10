"""Deprecated compatibility shim — moved to ``memorymaster.govern.steward_features``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.govern.steward_features``.
"""
import sys as _sys

from memorymaster.govern import steward_features as _new

_sys.modules[__name__] = _new
