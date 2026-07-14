"""MemoryMaster package metadata."""

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

try:
    __version__ = version("memorymaster")
except PackageNotFoundError:  # pragma: no cover - source tree without installation metadata
    __version__ = "0+unknown"
