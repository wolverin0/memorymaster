"""Standalone Hermes registration entrypoint for governed MemoryMaster memory."""

from .provider import MemoryMasterProvider

__all__ = ["MemoryMasterProvider", "register"]


def register(ctx) -> None:
    """Register the provider through Hermes' supported plugin context."""
    ctx.register_memory_provider(MemoryMasterProvider())
