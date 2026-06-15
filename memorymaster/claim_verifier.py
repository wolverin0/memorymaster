"""Deprecated compatibility shim — moved to ``memorymaster.govern.claim_verifier``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.govern.claim_verifier``.
"""
import sys as _sys

from memorymaster.govern import claim_verifier as _new

_sys.modules[__name__] = _new
