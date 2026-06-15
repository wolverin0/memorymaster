"""Deprecated compatibility shim — moved to ``memorymaster.surfaces.mcp_usage``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.surfaces.mcp_usage``.
"""
import sys as _sys

from memorymaster.surfaces import mcp_usage as _new

_sys.modules[__name__] = _new
