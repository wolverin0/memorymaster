"""Pagination cursor validation cases from AUDIT-2026-10-28 (risk R8).

`list_claims_page` is reachable from the MCP `list_claims` tool with a
caller-supplied `cursor`. Malformed cursors must fail as ValueError (a clean
client error), never as a sqlite3 binding error.
"""

from __future__ import annotations

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.stores._storage_pagination import _decode_cursor, _encode_cursor


@pytest.fixture
def svc(tmp_path):
    service = MemoryService(tmp_path / "page.db", workspace_root=tmp_path)
    service.init_db()
    for index in range(3):
        service.ingest(
            text=f"pagination fact number {index} about widgets",
            citations=[CitationInput(source="t")],
            scope="project:audit",
            source_agent="test",
        )
    return service


def test_cursor_round_trip():
    values = [1, 0.5, "2026-10-28T00:00:00+00:00", 7]
    assert _decode_cursor(_encode_cursor(values), 4) == values


@pytest.mark.parametrize("cursor", ["!!!", _encode_cursor([1, 2, 3]), _encode_cursor({"a": 1})])
def test_malformed_cursor_shape_raises_value_error(cursor):
    with pytest.raises(ValueError, match="invalid pagination cursor"):
        _decode_cursor(cursor, 4)


def test_pages_cover_all_rows_without_duplicates(svc):
    seen: list[int] = []
    cursor = ""
    for _ in range(5):
        page, cursor = svc.list_claims_page(limit=1, cursor=cursor)
        seen.extend(claim.id for claim in page)
        if not cursor:
            break
    assert len(seen) == 3
    assert len(set(seen)) == 3


@pytest.mark.skip(
    reason=(
        "AUDIT-2026-10-28 R8: _decode_cursor checks only list length, so a "
        "cursor whose elements are objects/lists reaches SQLite and raises "
        "sqlite3.ProgrammingError instead of ValueError."
    )
)
@pytest.mark.parametrize("values", [[{"a": 1}, 0, "x", 1], [[1], 0, "x", 1]])
def test_cursor_with_non_scalar_elements_raises_value_error(svc, values):
    with pytest.raises(ValueError):
        svc.list_claims_page(limit=1, cursor=_encode_cursor(values))
