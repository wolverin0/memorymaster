"""Deprecated compatibility shim — moved to ``memorymaster.knowledge.rule_export``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.knowledge.rule_export``.
"""
import sys as _sys

from memorymaster.knowledge import rule_export as _new

_sys.modules[__name__] = _new
