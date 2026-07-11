"""Red tests for secrets outside the claim's primary text fields.

The synthetic token is assembled at runtime and is not a credential.  Every
case exercises the canonical service ingest boundary with a temporary SQLite
database, then scans durable SQLite text values for the unredacted fixture.
"""
from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.security import SensitiveMetadataError, scan_text_for_findings
from memorymaster.core.service import MemoryService


def _synthetic_token() -> str:
    body = "".join(format((index * 7 + 3) % 16, "x") for index in range(40))
    token = "".join(("gh", "p_", body))
    assert "github_token" in scan_text_for_findings(token)
    return token


def _encoded_token() -> str:
    return base64.b64encode(_synthetic_token().encode()).decode()


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


def _table_row_count(db_path: Path, table: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _ingest_with_metadata_secret(service: MemoryService, field: str, secret: str) -> None:
    citation_source = secret if field == "citation_source" else "phase0-red-test"
    citation_locator = secret if field == "citation_locator" else "case:metadata"
    overrides = {
        "holder": {"holder": secret},
        "source_agent": {"source_agent": secret},
        "idempotency_key": {"idempotency_key": secret},
        "claim_type": {"claim_type": secret},
        "scope": {"scope": secret},
        "volatility": {"volatility": secret},
        "confidence": {"confidence": secret},
        "event_time": {"event_time": secret},
        "valid_from": {"valid_from": secret},
        "valid_until": {"valid_until": secret},
        "visibility": {"visibility": secret},
        "intake_batch_id": {"intake_batch_id": secret},
        "citation_source": {},
        "citation_locator": {},
    }[field]
    attribution = {} if field == "source_agent" else {"source_agent": "phase0-red-test"}
    metadata = {
        "scope": "project:phase0-red-test",
        **attribution,
        **overrides,
    }
    service.ingest(
        text=f"Benign metadata sensitivity case for {field}.",
        citations=[CitationInput(source=citation_source, locator=citation_locator)],
        **metadata,
    )


_METADATA_FIELDS = [
    "holder",
    "source_agent",
    "idempotency_key",
    "claim_type",
    "scope",
    "volatility",
    "confidence",
    "event_time",
    "valid_from",
    "valid_until",
    "visibility",
    "intake_batch_id",
    "citation_source",
    "citation_locator",
]


@pytest.mark.parametrize(
    ("field", "encoding"),
    [(field, "literal") for field in _METADATA_FIELDS]
    + [(field, "base64") for field in _METADATA_FIELDS],
)
def test_secret_shaped_metadata_never_reaches_durable_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    encoding: str,
) -> None:
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.delenv("MEMORYMASTER_ENCRYPTION_KEY", raising=False)
    db_path = tmp_path / f"persisted-envelope-{field}.db"
    service = MemoryService(db_path, workspace_root=tmp_path)
    service.init_db()
    secret = _synthetic_token() if encoding == "literal" else _encoded_token()

    with pytest.raises(SensitiveMetadataError) as rejected:
        _ingest_with_metadata_secret(service, field, secret)
    assert secret not in str(rejected.value), "validation errors must never echo secret values"

    assert _durable_locations(db_path, secret) == []
    assert _table_row_count(db_path, "claims") == 0
    assert _table_row_count(db_path, "citations") == 0


@pytest.mark.parametrize("identity_source", ["configured_default", "tenant_id", "bound_principal"])
def test_effective_identity_is_revalidated_before_claim_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_source: str,
) -> None:
    monkeypatch.delenv("QDRANT_URL", raising=False)
    secret = _synthetic_token()
    db_path = tmp_path / f"effective-identity-{identity_source}.db"
    initializer = MemoryService(db_path, workspace_root=tmp_path)
    initializer.init_db()
    kwargs: dict[str, object] = {}
    if identity_source == "configured_default":
        monkeypatch.setenv("MEMORYMASTER_INTAKE_REQUIRE_SOURCE_AGENT", "warn")
        monkeypatch.setenv("MEMORYMASTER_INTAKE_DEFAULT_SOURCE_AGENT", secret)
    elif identity_source == "tenant_id":
        kwargs["tenant_id"] = secret
    else:
        kwargs.update(
            tenant_id="tenant-safe",
            require_tenant=True,
            principal=secret,
            allowed_scopes=["project:phase0-red-test"],
        )
    service = MemoryService(db_path, workspace_root=tmp_path, **kwargs)

    with pytest.raises(SensitiveMetadataError) as rejected:
        service.ingest(
            text="Benign effective identity sensitivity case.",
            citations=[CitationInput(source="phase0-red-test", locator="effective-identity")],
            scope="project:phase0-red-test",
        )

    assert secret not in str(rejected.value)
    assert _durable_locations(db_path, secret) == []
    assert _table_row_count(db_path, "claims") == 0
    assert _table_row_count(db_path, "citations") == 0


@pytest.mark.parametrize(
    "field",
    ["text", "subject", "predicate", "object_value", "citation_excerpt"],
)
def test_encoded_secret_in_claim_content_never_reaches_durable_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.delenv("MEMORYMASTER_ENCRYPTION_KEY", raising=False)
    db_path = tmp_path / f"persisted-envelope-content-{field}.db"
    service = MemoryService(db_path, workspace_root=tmp_path)
    service.init_db()
    encoded_secret = _encoded_token()
    values = {
        "text": "Benign encoded-secret field test.",
        "subject": "safe-subject",
        "predicate": "safe-predicate",
        "object_value": "safe-object",
        "citation_excerpt": "safe excerpt",
    }
    values[field] = encoded_secret

    claim = service.ingest(
        text=values["text"],
        subject=values["subject"],
        predicate=values["predicate"],
        object_value=values["object_value"],
        citations=[
            CitationInput(
                source="phase0-red-test",
                locator="case:encoded-content",
                excerpt=values["citation_excerpt"],
            )
        ],
        scope="project:phase0-red-test",
        source_agent="phase0-red-test",
    )

    assert _durable_locations(db_path, encoded_secret) == []
    assert _durable_locations(db_path, _synthetic_token()) == []
    if field == "citation_excerpt":
        assert _durable_locations(db_path, "[REDACTED:encoded_secret]")
    else:
        assert getattr(claim, field) == "[REDACTED:encoded_secret]"
