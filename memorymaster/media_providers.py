"""Deprecated compatibility shim — moved to ``memorymaster.bridges.media_providers``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.bridges.media_providers``.
"""
import sys as _sys

from memorymaster.bridges import media_providers as _new

_sys.modules[__name__] = _new
