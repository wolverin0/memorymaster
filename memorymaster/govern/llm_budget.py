"""Deprecated compatibility shim — moved to ``memorymaster.core.llm_budget``.

Cycle-tidy follow-up to the P2 restructure: ``llm_budget`` is a low-level
LLM-call budget gate (a sibling of ``core.llm_provider``), so it belongs in
``core``, not ``govern``. Leaving it in ``govern`` made the foundational
``core.llm_provider`` import upward into ``govern`` — a load-time
``core -> govern`` edge. This alias keeps the interim ``govern.llm_budget``
path working for one minor version. Update imports to
``memorymaster.core.llm_budget``.
"""
import sys as _sys

from memorymaster.core import llm_budget as _new

_sys.modules[__name__] = _new
