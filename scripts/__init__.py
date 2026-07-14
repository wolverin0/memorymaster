"""Repository-local maintenance scripts.

This package marker prevents an unrelated installed ``scripts`` distribution
from shadowing these modules during tests. Setuptools excludes ``scripts*``
from the MemoryMaster wheel, so this remains repository-only tooling.
"""
