"""Deprecated compatibility shim — folded into ``memorymaster.stores._storage_schema``.

P2 restructure: ``load_schema_sql`` / ``load_schema_postgres_sql`` now live in
``memorymaster.stores._storage_schema``. This alias keeps the old import path
working for one minor version. Update imports accordingly.
"""
import sys as _sys

from memorymaster.stores import _storage_schema as _new

_sys.modules[__name__] = _new
