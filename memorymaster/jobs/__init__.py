"""Deprecated compatibility shim — moved to ``memorymaster.govern.jobs``.

P2 restructure: this alias keeps the old package path and its submodules
(``memorymaster.jobs.decay`` etc.) working for one minor version.
Update imports to ``memorymaster.govern.jobs``.
"""
import importlib as _importlib
import sys as _sys

from memorymaster.govern import jobs as _new

_sys.modules[__name__] = _new

for _sub in (
    "calibration",
    "compact_summaries",
    "compactor",
    "daydream_ingest",
    "decay",
    "dedup",
    "deterministic",
    "entity_graph_export",
    "extractor",
    "fk_repair",
    "integrity",
    "qdrant_reconcile",
    "spool_drain",
    "staleness",
    "validator",
):
    _sys.modules[__name__ + "." + _sub] = _importlib.import_module(
        "memorymaster.govern.jobs." + _sub
    )
