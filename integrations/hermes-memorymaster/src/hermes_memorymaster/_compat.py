"""Hermes ABI import with a narrow test-only fallback when Hermes is absent."""

from __future__ import annotations

from typing import Any

try:
    from agent.memory_provider import MemoryProvider

    HERMES_ABI_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only outside Hermes installations
    HERMES_ABI_AVAILABLE = False

    class MemoryProvider:  # type: ignore[no-redef]
        """Minimal import fallback; Hermes supplies the real ABC in production."""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            super().__init__()
