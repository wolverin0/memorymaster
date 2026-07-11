"""Red tests for secrets outside the claim's primary text fields.

The synthetic token is assembled at runtime and is not a credential.  Every
case exercises the canonical service ingest boundary with a temporary SQLite
database, then scans durable SQLite text values for the unredacted fixture.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.security import scan_text_for_findings
from memorymaster.core.service import MemoryService


def _synthetic_token() -> str:
    body = "".join(format((index * 7 + 3) % 16, "x") for index in range(40))
    token = "".join(("gh", "p_", body))
    assert "github_token" in scan_text_for_findings(token)
    return token


def _durable_locations(db_path: Path, needle: str) -> list[str]:
    locations: list[str] = []
    with sqlite3.connect(db_path) as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (table,) in tables:
            quoted_table = '"' + str(table).replace('"', '""') + '"'
            columns = conn.execute(f"PRAGMA table_info({quoted_table})").fetchall()
            for column in (str(row[1]) for row in columns):
                quoted_column = '"' + column.replace('"', '""') + '"'
                values = conn.execute(
                    f"SELECT {quoted_column} FROM {quoted_table} WHERE {quoted_column} IS NOT NULL"
                ).fetchall()
                if any(isinstance(value, str) and needle in value for (value,) in values):
                    locations.append(f"{table}.{column}")
    return locations


def _ingest_with_metadata_secret(service: MemoryService, field: str, secret: str) -> None:
    citation_source = secret if field == "citation_source" else "phase0-red-test"
    citation_locator = secret if field == "citation_locator" else "case:metadata"
    overrides = {
        "holder": {"holder": secret},
        "source_agent": {"source_agent": secret},
        "idempotency_key": {"idempotency_key": secret},
        "citation_source": {},
        "citation_locator": {},
    }[field]
    attribution = {} if field == "source_agent" else {"source_agent": "phase0-red-test"}
    service.ingest(
        text=f"Benign metadata sensitivity case for {field}.",
        citations=[CitationInput(source=citation_source, locator=citation_locator)],
        scope="project:phase0-red-test",
        **(attribution | overrides),
    )


@pytest.mark.xfail(
    strict=True,
    reason="MM-SEC-03: persisted-envelope metadata bypasses the sensitivity gateway",
)
@pytest.mark.parametrize(
    "field",
    ["holder", "source_agent", "idempotency_key", "citation_source", "citation_locator"],
)
def test_secret_shaped_metadata_never_reaches_durable_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.delenv("MEMORYMASTER_ENCRYPTION_KEY", raising=False)
    db_path = tmp_path / f"persisted-envelope-{field}.db"
    service = MemoryService(db_path, workspace_root=tmp_path)
    service.init_db()
    secret = _synthetic_token()

    _ingest_with_metadata_secret(service, field, secret)

    assert _durable_locations(db_path, secret) == []
