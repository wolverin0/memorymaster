"""Deprecated compatibility shim — moved to ``memorymaster.govern.rl_trainer``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.govern.rl_trainer``.
"""
import sys as _sys

from memorymaster.govern import rl_trainer as _new

_sys.modules[__name__] = _new
