"""Deprecated compatibility shim — moved to ``memorymaster.knowledge.vault_curator``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.knowledge.vault_curator``.
"""
import sys as _sys

from memorymaster.knowledge import vault_curator as _new

_sys.modules[__name__] = _new
