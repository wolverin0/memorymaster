"""Deprecated compatibility shim — moved to ``memorymaster.knowledge.wiki_engine``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.knowledge.wiki_engine``.
"""
import sys as _sys

from memorymaster.knowledge import wiki_engine as _new

_sys.modules[__name__] = _new
