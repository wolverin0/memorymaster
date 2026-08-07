"""Hermes directory shim for the installed MemoryMaster provider package."""

from hermes_memorymaster import MemoryMasterProvider


def register(ctx) -> None:
    """Register through the marker recognized by Hermes user-provider discovery."""
    ctx.register_memory_provider(MemoryMasterProvider())

__all__ = ["MemoryMasterProvider", "register"]
