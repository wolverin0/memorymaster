"""Vector hook for ``MemoryService.query_rows`` that degrades to lexical ranking (T-2004)."""

from __future__ import annotations

import logging
from typing import Any

from memorymaster.recall.embeddings import EmbeddingProvider, EncoderUnavailable

logger = logging.getLogger("memorymaster.core.service")


def build_vector_hook(store: Any, provider: EmbeddingProvider, query_text: str) -> tuple[Any, bool]:
    """Return ``(vector_hook, semantic)`` for ``query_text``.

    The MCP servers run the model in a child process that can die (OOM, watchdog): when the query
    or a claim embedding is unavailable, rank lexically with a warning instead of failing the query.
    The query vector is computed once and handed to storage, avoiding a second provider embedding.
    """
    try:
        query_vector = provider.embed(query_text)
        semantic = provider.is_semantic
    except EncoderUnavailable as exc:
        logger.warning("query embedding unavailable (%s); ranking lexically", exc)
        return None, False

    def vector_hook(text, claims):
        try:
            return store.vector_scores(text, claims, provider, query_vector=query_vector)
        except EncoderUnavailable as exc:  # embedding stale claims needs the child too
            logger.warning("claim embedding unavailable (%s); ranking lexically", exc)
            return {}

    return vector_hook, semantic
