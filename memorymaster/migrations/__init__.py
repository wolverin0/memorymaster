"""Deprecated compatibility shim — moved to ``memorymaster.stores.migrations``.

P2 restructure: this alias keeps the old package path and its submodules
(``memorymaster.migrations.runner``, importlib-loaded ``NNNN_*`` migration
modules) working for one minor version. Update imports to
``memorymaster.stores.migrations``.
"""
import importlib as _importlib
import sys as _sys

from memorymaster.stores import migrations as _new

_sys.modules[__name__] = _new

_sys.modules[__name__ + ".runner"] = _importlib.import_module(
    "memorymaster.stores.migrations.runner"
)
