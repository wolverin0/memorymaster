"""Deprecated compatibility shim — moved to ``memorymaster.bridges.federated_graphify``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.bridges.federated_graphify``.
"""
import sys as _sys

from memorymaster.bridges import federated_graphify as _new

_sys.modules[__name__] = _new
