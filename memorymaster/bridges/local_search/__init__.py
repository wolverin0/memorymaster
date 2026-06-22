"""Local filesystem search bridge.

Provides a governed, privacy-safe path-lookup layer for AI agents:
- LocalSearchProvider protocol + shared DTOs (provider.py)
- EverythingProvider: Windows Everything ES.exe wrapper (everything.py)
- resolve_project(): alias -> canonical on-disk path with confidence (resolver.py)
- collapse_path / expand_path: root-relative tokenisation for safe ingest (redact.py)
"""
from __future__ import annotations

from memorymaster.bridges.local_search.provider import (
    LocalSearchProvider,
    PathHit,
    ResolveMatch,
    ResolveResult,
)

__all__ = [
    "LocalSearchProvider",
    "PathHit",
    "ResolveMatch",
    "ResolveResult",
]
