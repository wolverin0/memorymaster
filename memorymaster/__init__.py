"""MemoryMaster package metadata."""

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__", "forget", "improve", "recall", "remember"]

try:
    __version__ = version("memorymaster")
except PackageNotFoundError:  # pragma: no cover - source tree without installation metadata
    __version__ = "0+unknown"


def remember(*args, **kwargs):
    from memorymaster.public.v1 import remember as operation

    return operation(*args, **kwargs)


def recall(*args, **kwargs):
    from memorymaster.public.v1 import recall as operation

    return operation(*args, **kwargs)


def forget(*args, **kwargs):
    from memorymaster.public.v1 import forget as operation

    return operation(*args, **kwargs)


def improve(*args, **kwargs):
    from memorymaster.public.v1 import improve as operation

    return operation(*args, **kwargs)
