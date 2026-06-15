"""Deprecated compatibility shim — moved to ``memorymaster.knowledge.daily_notes``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.knowledge.daily_notes``.
"""
import sys as _sys

from memorymaster.knowledge import daily_notes as _new

_sys.modules[__name__] = _new
