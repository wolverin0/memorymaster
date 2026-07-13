from __future__ import annotations

import base64
import logging
import os
import sqlite3
from pathlib import Path

import pytest

import memorymaster.bridges.delta_sync as delta_sync_module
from memorymaster.bridges.delta_sync import _copy_table_ddl, export_delta
from memorymaster.core.models import CitationInput
from memorymaster.core.security import SensitiveMetadataError
from memorymaster.core.service import MemoryService


LITERAL = "OPENAI_API_KEY=sk-proj-FAKEbridgeEnvelope1234567890ABCD"
ENCODED = base64.b64encode(LITERAL.encode()).decode()


def _service(path: Path, workspace: Path) -> MemoryService:
    service = MemoryService(path, workspace_root=workspace)
    service.init_db()
    return service


def _claim(service: MemoryService, text: str = "Safe bridge claim"):
    return service.ingest(
        text,
        [CitationInput(source="test://bridge", locator="line-1", excerpt="safe")],
        source_agent="bridge-test",
    )


def _update(path: Path, statement: str, values: tuple[object, ...]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(statement, values)


def _column(path: Path, table: str, field: str) -> object:
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT {field} FROM {table}").fetchone()[0]


def test_delta_refuses_source_as_output_before_unlink(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    service = _service(source, tmp_path)
    _claim(service)
    before = source.read_bytes()

    with pytest.raises(ValueError, match="alias"):
        export_delta(source, "", source)

    assert source.exists()
    assert source.read_bytes() == before


def test_delta_refuses_resolved_source_alias_before_unlink(tmp_path: Path) -> None:
    directory = tmp_path / "nested"
    directory.mkdir()
    source = tmp_path / "source.db"
    service = _service(source, tmp_path)
    _claim(service)
    alias = directory / ".." / source.name

    with pytest.raises(ValueError, match="alias"):
        export_delta(source, "", alias)

    assert source.exists()


def test_delta_refuses_hardlink_to_source_before_unlink(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    alias = tmp_path / "hardlink.db"
    service = _service(source, tmp_path)
    _claim(service)
    os.link(source, alias)

    with pytest.raises(ValueError, match="alias"):
        export_delta(source, "", alias)

    assert source.exists()
    assert alias.exists()


def test_delta_never_executes_forged_source_schema_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = sqlite3.connect(":memory:")
    output = sqlite3.connect(":memory:")
    source.execute("CREATE TABLE claims (id INTEGER PRIMARY KEY, text TEXT)")
    source.execute("PRAGMA writable_schema=ON")
    source.execute(
        "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'claims'",
        ("ATTACH DATABASE 'forged-side-effect.db' AS injected",),
    )
    source.commit()
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="canonical transport schema"):
        _copy_table_ddl(source, output, "claims")

    assert not (tmp_path / "forged-side-effect.db").exists()


def test_delta_quotes_reserved_identifiers_in_value_copy_statements(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy-source.db"
    output = tmp_path / "delta.db"
    with sqlite3.connect(source) as conn:
        conn.executescript(
            """
            CREATE TABLE claims (
                id INTEGER PRIMARY KEY,
                text TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                "select" TEXT
            );
            CREATE TABLE citations (
                id INTEGER PRIMARY KEY,
                claim_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                "from" TEXT
            );
            INSERT INTO claims (id, text, updated_at, "select")
            VALUES (1, 'safe legacy claim', '2026-07-11T00:00:00+00:00', 'claim-extra');
            INSERT INTO citations (id, claim_id, source, created_at, "from")
            VALUES (1, 1, 'test://bridge', '2026-07-11T00:00:00+00:00', 'citation-extra');
            """
        )

    result = export_delta(source, "", output)

    assert result["exported"] == 1
    assert result["citations"] == 1
    assert _column(output, "claims", '"select"') == "claim-extra"
    assert _column(output, "citations", '"from"') == "citation-extra"


@pytest.mark.parametrize(
    "table,field",
    [
        ("claims", "idempotency_key"),
        ("claims", "claim_type"),
        ("claims", "scope"),
        ("claims", "volatility"),
        ("claims", "created_at"),
        ("claims", "updated_at"),
        ("claims", "source_agent"),
        ("claims", "wiki_article"),
        ("claims", "holder"),
        ("claims", "tenant_id"),
        ("citations", "source"),
        ("citations", "locator"),
        ("citations", "created_at"),
    ],
)
@pytest.mark.parametrize("secret", [LITERAL, ENCODED])
def test_delta_rejects_unsafe_metadata_without_persisting_it(
    tmp_path: Path, table: str, field: str, secret: str
) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "delta.db"
    service = _service(source, tmp_path)
    claim = _claim(service)
    where = "id = ?" if table == "claims" else "claim_id = ?"
    _update(source, f"UPDATE {table} SET {field} = ? WHERE {where}", (secret, claim.id))

    result = export_delta(source, "", output)

    assert result["exported"] == 0
    assert result["citations"] == 0
    assert result["rejected"] == 1
    assert _column(output, "claims", "COUNT(*)") == 0
    assert secret.encode() not in output.read_bytes()


@pytest.mark.parametrize(
    "field", ["text", "normalized_text", "subject", "predicate", "object_value"]
)
@pytest.mark.parametrize("secret", [LITERAL, ENCODED])
def test_delta_sanitizes_every_claim_content_field(
    tmp_path: Path, field: str, secret: str
) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "delta.db"
    service = _service(source, tmp_path)
    claim = _claim(service)
    _update(source, f"UPDATE claims SET {field} = ? WHERE id = ?", (secret, claim.id))

    result = export_delta(source, "", output)

    assert result["exported"] == 1
    assert "[REDACTED:" in str(_column(output, "claims", field))
    assert secret.encode() not in output.read_bytes()


@pytest.mark.parametrize("secret", [LITERAL, ENCODED])
def test_delta_sanitizes_citation_excerpt(tmp_path: Path, secret: str) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "delta.db"
    service = _service(source, tmp_path)
    claim = _claim(service)
    _update(
        source,
        "UPDATE citations SET excerpt = ? WHERE claim_id = ?",
        (secret, claim.id),
    )

    result = export_delta(source, "", output)

    assert result["exported"] == 1
    assert "[REDACTED:" in str(_column(output, "citations", "excerpt"))
    assert secret.encode() not in output.read_bytes()


def test_delta_rejects_secret_bearing_transport_ddl_before_output_write(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy-source.db"
    output = tmp_path / "delta.db"
    with sqlite3.connect(source) as conn:
        conn.executescript(
            f"""
            CREATE TABLE claims (
                id INTEGER PRIMARY KEY,
                text TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                leaked_default TEXT DEFAULT '{LITERAL}'
            );
            CREATE TABLE citations (
                id INTEGER PRIMARY KEY,
                claim_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                locator TEXT,
                excerpt TEXT,
                created_at TEXT NOT NULL
            );
            INSERT INTO claims (id, text, updated_at)
            VALUES (1, 'safe legacy claim', '2026-07-11T00:00:00+00:00');
            """
        )

    with pytest.raises(SensitiveMetadataError):
        export_delta(source, "", output)

    assert not output.exists() or LITERAL.encode() not in output.read_bytes()


def test_delta_rejects_unsafe_legacy_identifier_without_echo(tmp_path: Path) -> None:
    source = tmp_path / "legacy-source.db"
    output = tmp_path / "delta.db"
    with sqlite3.connect(source) as conn:
        conn.executescript(
            """
            CREATE TABLE claims (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE citations (
                id INTEGER PRIMARY KEY,
                claim_id TEXT NOT NULL,
                source TEXT NOT NULL,
                locator TEXT,
                excerpt TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO claims (id, text, updated_at) VALUES (?, 'safe', ?)",
            (LITERAL, "2026-07-11T00:00:00+00:00"),
        )

    with pytest.raises(SensitiveMetadataError) as rejected:
        export_delta(source, "", output)

    assert LITERAL not in str(rejected.value)
    assert not output.exists() or LITERAL.encode() not in output.read_bytes()


def test_delta_rejects_secret_bearing_watermark_without_echo(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "delta.db"
    service = _service(source, tmp_path)
    _claim(service)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(SensitiveMetadataError) as rejected:
            export_delta(source, LITERAL, output)

    assert LITERAL not in str(rejected.value)
    assert LITERAL not in caplog.text
    assert not output.exists() or LITERAL.encode() not in output.read_bytes()


def test_delta_reads_claims_and_citations_from_one_source_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "delta.db"
    service = _service(source, tmp_path)
    claim = _claim(service, "Snapshot-safe delta citation")
    original = delta_sync_module._load_citations_by_claim
    changed = False

    def mutate_then_read(conn: sqlite3.Connection, claim_ids: list[int]):
        nonlocal changed
        if not changed:
            changed = True
            _update(
                source,
                "UPDATE citations SET source = ? WHERE claim_id = ?",
                (LITERAL, claim.id),
            )
        return original(conn, claim_ids)

    monkeypatch.setattr(
        delta_sync_module,
        "_load_citations_by_claim",
        mutate_then_read,
    )

    result = export_delta(source, "", output)

    assert result["exported"] == 1
    assert _column(output, "citations", "source") == "test://bridge"


def test_delta_rejects_secret_bearing_binary_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "delta.db"
    service = _service(source, tmp_path)
    claim = _claim(service)
    _update(
        source,
        "UPDATE claims SET wiki_article = ? WHERE id = ?",
        (sqlite3.Binary(LITERAL.encode()), claim.id),
    )

    result = export_delta(source, "", output)

    assert result["exported"] == 0
    assert result["rejected"] == 1
    assert LITERAL.encode() not in output.read_bytes()


def test_delta_decodes_and_sanitizes_secret_bearing_binary_content(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "delta.db"
    service = _service(source, tmp_path)
    claim = _claim(service)
    _update(
        source,
        "UPDATE claims SET text = ? WHERE id = ?",
        (sqlite3.Binary(LITERAL.encode()), claim.id),
    )

    result = export_delta(source, "", output)

    assert result["exported"] == 1
    assert "[REDACTED:" in str(_column(output, "claims", "text"))
    assert LITERAL.encode() not in output.read_bytes()
