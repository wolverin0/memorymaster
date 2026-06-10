"""Deprecated compatibility shim — moved to ``memorymaster.bridges.atlas_claim_extractor``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.bridges.atlas_claim_extractor``.
"""
import sys as _sys

from memorymaster.bridges import atlas_claim_extractor as _new

_sys.modules[__name__] = _new
