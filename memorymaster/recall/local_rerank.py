"""Optional local cross-encoder rerank for the recall hook path.

Gate: ``MEMORYMASTER_RECALL_RERANK_LOCAL=1`` — default OFF. When the gate
is off, ``recall()`` never imports this module's model stack (torch /
sentence_transformers stay unloaded) and recall output is byte-identical
to legacy. When on, ``recall()`` over-fetches ~3x FTS candidates, this
module reranks the fused candidate pool with a local cross-encoder, and
the pool is trimmed back to the usual count before rendering.

Model: ``cross-encoder/ms-marco-MiniLM-L-6-v2`` via
``sentence_transformers.CrossEncoder`` (CPU, ~90MB download on first use).
The model is loaded lazily on the first scored call and cached per
process; a load failure latches the reranker off for the rest of the
process so a broken torch install degrades to legacy ordering instead of
crashing the hook.

Prior art: the RRF fusion experiment regressed MAP@5 by 49% when shipped
unmeasured — every reordering pass must be measured on the real harness
(scripts/eval_recall_precision_at_5.py) before being recommended on.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)

_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"
# Cap on candidate text length fed to the cross-encoder — mirrors the
# 300-char snippet cap used by llm_rerank and the render loop.
_MAX_TEXT_CHARS = 300

_model: Any | None = None
_model_failed = False

_STATS: dict[str, int] = {"attempts": 0, "successes": 0, "failures": 0}


def local_rerank_enabled() -> bool:
    """Gate for the local cross-encoder rerank pass. Default OFF."""
    return os.environ.get("MEMORYMASTER_RECALL_RERANK_LOCAL", "").strip() == "1"


def overfetch_factor() -> int:
    """How many times the legacy FTS candidate cap to fetch when the gate
    is on (default 3). Clamped to >= 1; only consulted when
    :func:`local_rerank_enabled` is true.
    """
    raw = os.environ.get("MEMORYMASTER_RECALL_RERANK_LOCAL_OVERFETCH", "3").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 3


def _max_pairs() -> int:
    """Safety cap on (query, text) pairs scored per recall() call."""
    raw = os.environ.get("MEMORYMASTER_RECALL_RERANK_LOCAL_MAX_PAIRS", "48").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 48


def _get_model() -> Any | None:
    """Lazy-load the cross-encoder once per process; latch off on failure."""
    global _model, _model_failed
    if _model is not None:
        return _model
    if _model_failed:
        return None
    try:
        from sentence_transformers import CrossEncoder

        _model = CrossEncoder(_MODEL_NAME)
    except Exception as exc:
        _model_failed = True
        logger.warning(
            "local rerank: CrossEncoder load failed — disabled for this "
            "process, recall keeps legacy ordering: %s", exc,
        )
        return None
    return _model


def _row_text(row: Any) -> str:
    """Extract candidate text for scoring from a recall row dict."""
    claim = row.get("claim") if isinstance(row, dict) else None
    subject = str(getattr(claim, "subject", "") or "")
    text = str(getattr(claim, "text", "") or "")
    joined = f"{subject} {text}".strip()
    return " ".join(joined.split())[:_MAX_TEXT_CHARS]


def score_pairs(query: str, texts: list[str]) -> list[float] | None:
    """Score (query, text) pairs with the local cross-encoder.

    Returns None on any failure (model missing, predict error) so callers
    can fall back to the input ordering. Tests monkeypatch this function
    to keep torch out of the test process entirely.
    """
    model = _get_model()
    if model is None:
        return None
    try:
        scores = model.predict([(query, t) for t in texts])
        return [float(s) for s in scores]
    except Exception as exc:
        logger.warning("local rerank: predict failed, keeping input order: %s", exc)
        return None


def rerank_ranked_rows(
    query: str,
    ranked: list[Any],
    *,
    score_fn: Callable[[str, list[str]], list[float] | None] | None = None,
) -> list[Any]:
    """Rerank the head of ``ranked`` by cross-encoder relevance.

    Scores the top ``MEMORYMASTER_RECALL_RERANK_LOCAL_MAX_PAIRS`` rows in
    one batched predict call and reorders them by descending score
    (stable on ties — original rank breaks ties). Rows beyond the scored
    head keep their original relative order after it. Any scoring failure
    returns ``ranked`` unchanged (same object) so the caller's ordering
    is never corrupted by a half-applied rerank.
    """
    if not query.strip() or len(ranked) <= 1:
        return ranked
    pool_size = min(len(ranked), _max_pairs())
    pool = ranked[:pool_size]
    texts = [_row_text(r) for r in pool]

    _STATS["attempts"] += 1
    scorer = score_fn if score_fn is not None else score_pairs
    scores = scorer(query, texts)
    if scores is None or len(scores) != len(pool):
        _STATS["failures"] += 1
        return ranked

    _STATS["successes"] += 1
    order = sorted(range(pool_size), key=lambda i: (-scores[i], i))
    return [pool[i] for i in order] + list(ranked[pool_size:])


def get_local_rerank_stats() -> dict[str, int]:
    """Telemetry snapshot (attempts / successes / failures + latch state)."""
    return {**_STATS, "model_failed": int(_model_failed)}
