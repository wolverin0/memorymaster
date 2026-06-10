"""Deprecated compatibility shim — moved to ``memorymaster.govern.candidate_dedupe``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.govern.candidate_dedupe``.
"""
import sys as _sys

from memorymaster.govern import candidate_dedupe as _new

_sys.modules[__name__] = _new
