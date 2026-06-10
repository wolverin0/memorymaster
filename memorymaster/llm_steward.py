"""Deprecated compatibility shim — moved to ``memorymaster.govern.llm_steward``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.govern.llm_steward``.
"""
import sys as _sys

from memorymaster.govern import llm_steward as _new

_sys.modules[__name__] = _new
