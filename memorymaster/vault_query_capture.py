"""Deprecated compatibility shim — moved to ``memorymaster.knowledge.vault_query_capture``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.knowledge.vault_query_capture``.
"""
import sys as _sys

from memorymaster.knowledge import vault_query_capture as _new

_sys.modules[__name__] = _new
