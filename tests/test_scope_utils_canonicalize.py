"""canonicalize_slug parity tests.

Anchors the public ``core.scope_utils.canonicalize_slug`` to the canonical
behaviour of ``surfaces/mcp_server._canonicalize_slug`` (the original source).
The resolver depends on these collapsing exactly, so a divergence here is a
correctness bug, not a style nit: ``whatsappbot-final`` and ``whatsappbot``
MUST land in the same project scope or the memory loop fragments.
"""
from __future__ import annotations

import pytest

from memorymaster.core.scope_utils import canonicalize_slug
from memorymaster.surfaces.mcp_server import _canonicalize_slug


@pytest.mark.unit
@pytest.mark.parametrize(
    "dirname,expected",
    [
        ("memorymaster", "memorymaster"),
        ("MemoryMaster", "memorymaster"),
        ("Foo - Copy", "foo"),
        ("Foo - Copy - Copy", "foo"),
        ("project (1)", "project"),
        ("whatsappbot-final", "whatsappbot"),
        ("whatsappbot-prod", "whatsappbot"),
        ("_omniclaude", "omniclaude"),
        ("", "workspace"),
        ("  ", "workspace"),
    ],
)
def test_canonicalize_known_cases(dirname: str, expected: str) -> None:
    """Public helper produces the documented slug for each known input."""
    assert canonicalize_slug(dirname) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "dirname",
    [
        "memorymaster",
        "MemoryMaster",
        "Foo - Copy",
        "Foo - Copy - Copy",
        "project (1)",
        "whatsappbot-final",
        "whatsappbot-prod",
        "_omniclaude",
        "Delta Exchange",
        "my_project-staging",
        "todomax (2)",
        "",
        "   ",
    ],
)
def test_matches_mcp_server_behaviour(dirname: str) -> None:
    """The public helper must agree byte-for-byte with the original mcp_server impl."""
    assert canonicalize_slug(dirname) == _canonicalize_slug(dirname)


@pytest.mark.unit
def test_exported_in_all() -> None:
    """canonicalize_slug is part of the public scope_utils surface."""
    from memorymaster.core import scope_utils

    assert "canonicalize_slug" in scope_utils.__all__
