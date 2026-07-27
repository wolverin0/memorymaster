"""Recall layer: retrieval, ranking, embeddings, vector/graph/verbatim recall.

P2 restructure subpackage. Hosts the production recall hook (context_hook),
FTS/vector/graph candidate gathering, RRF fusion and ranking, query
classification/expansion/caching, embeddings, Qdrant backends, and verbatim
transcript recall.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any


class _CallableRecallModule(ModuleType):
    """Preserve the friendly facade after Python imports this subpackage."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        from memorymaster.public.v1 import recall as operation

        return operation(*args, **kwargs)


sys.modules[__name__].__class__ = _CallableRecallModule
