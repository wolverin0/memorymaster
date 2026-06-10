"""Deprecated compatibility shim — moved to ``memorymaster.recall.llm_rerank``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.recall.llm_rerank``.
"""
import sys as _sys

from memorymaster.recall import llm_rerank as _new

_sys.modules[__name__] = _new
