"""Deprecated compatibility shim — moved to ``memorymaster.core.hook_log``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.core.hook_log``.
"""
import sys as _sys

from memorymaster.core import hook_log as _new

_sys.modules[__name__] = _new
