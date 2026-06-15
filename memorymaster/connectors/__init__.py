"""Deprecated compatibility shim — moved to ``memorymaster.bridges.connectors``.

P2 restructure: this alias keeps the old package path and its submodules
(``memorymaster.connectors.whatsapp``) working for one minor version.
Update imports to ``memorymaster.bridges.connectors``.
"""
import sys as _sys

from memorymaster.bridges import connectors as _new
from memorymaster.bridges.connectors import whatsapp as _whatsapp

_sys.modules[__name__] = _new
_sys.modules[__name__ + ".whatsapp"] = _whatsapp
