"""Deprecated compatibility shim — moved to ``memorymaster.knowledge.rule_miner``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.knowledge.rule_miner``.
"""
import sys as _sys

from memorymaster.knowledge import rule_miner as _new

_sys.modules[__name__] = _new
