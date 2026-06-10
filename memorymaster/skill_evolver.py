"""Deprecated compatibility shim — moved to ``memorymaster.knowledge.skill_evolver``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.knowledge.skill_evolver``.
"""
import sys as _sys

from memorymaster.knowledge import skill_evolver as _new

_sys.modules[__name__] = _new
