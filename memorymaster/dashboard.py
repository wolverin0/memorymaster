"""Deprecated compatibility shim — moved to ``memorymaster.surfaces.dashboard``.

P2 restructure: this alias keeps the old import path (including submodule
attribute access) working for one minor version. Update imports to
``memorymaster.surfaces.dashboard``.
"""
import sys as _sys

from memorymaster.surfaces import dashboard as _new

_sys.modules[__name__] = _new

if __name__ == "__main__":  # pragma: no cover — `python -m memorymaster.dashboard` passthrough
    raise SystemExit(_new.main())
