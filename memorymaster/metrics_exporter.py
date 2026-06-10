"""Deprecated compatibility shim — moved to ``memorymaster.surfaces.metrics_exporter``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.surfaces.metrics_exporter``.
"""
import sys as _sys

from memorymaster.surfaces import metrics_exporter as _new

_sys.modules[__name__] = _new
