"""Deprecated compatibility shim — moved to ``memorymaster.core.models``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.core.models``.
"""
import sys as _sys

from memorymaster.core import models as _new

_sys.modules[__name__] = _new
