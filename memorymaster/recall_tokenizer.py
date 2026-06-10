"""Deprecated compatibility shim — moved to ``memorymaster.recall.recall_tokenizer``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.recall.recall_tokenizer``.
"""
import sys as _sys

from memorymaster.recall import recall_tokenizer as _new

_sys.modules[__name__] = _new
