"""Deprecated compatibility shim — moved to ``memorymaster.surfaces.session_tracker``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.surfaces.session_tracker``.
"""
import sys as _sys

from memorymaster.surfaces import session_tracker as _new

_sys.modules[__name__] = _new
