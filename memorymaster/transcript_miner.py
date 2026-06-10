"""Deprecated compatibility shim — moved to ``memorymaster.knowledge.transcript_miner``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.knowledge.transcript_miner``.
"""
import sys as _sys

from memorymaster.knowledge import transcript_miner as _new

_sys.modules[__name__] = _new
