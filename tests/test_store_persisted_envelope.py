"""Adversarial RED tests for the low-level persisted-envelope gateway.

The fixtures are synthetic, assembled at runtime, and written only to temporary
databases.  These tests intentionally exercise store APIs directly so callers
cannot bypass the canonical service ingest boundary.
"""
from __future__ import annotations

import base64
import copy
import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.security import SensitiveMetadataError, scan_text_for_findings
from memorymaster.stores.postgres_store import PostgresStore
from memorymaster.stores.storage import SQLiteStore


def _literal_secret() -> str:
    body = "".join(format((index * 7 + 3) % 16, "x") for index in range(40))
    token = "".join(("gh", "p_", body))
    assert "github_token" in scan_text_for_findings(token)
    return token


def _secret(encoding: str) -> str:
    literal = _literal_secret()
    if encoding == "literal":
        return literal
    encoded = base64.b64encode(literal.encode()).decode()
    assert "github_token" in scan_text_for_findings(encoded)
    return encoded


def _needles(secret: str) -> tuple[str, ...]:
    literal = _literal_secret()
    return (secret,) if secret == literal else (secret, literal)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _durable_locations(db_path: Path, needle: str) -> list[str]:
    locations: list[str] = []
    with sqlite3.connect(db_path) as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (raw_table,) in tables:
            table = str(raw_table)
            quoted_table = _quote_identifier(table)
            columns = conn.execute(f"PRAGMA table_xinfo({quoted_table})").fetchall()
            for raw_column in (row[1] for row in columns):
                column = str(raw_column)
                quoted_column = _quote_identifier(column)
                values = conn.execute(
                    f"SELECT {quoted_column} FROM {quoted_table} "
                    f"WHERE {quoted_column} IS NOT NULL"
                ).fetchall()
                if any(isinstance(value, str) and needle in value for (value,) in values):
                    locations.append(f"{table}.{column}")
    return locations


def _assert_absent_everywhere(db_path: Path, secret: str) -> None:
    leaked: dict[str, list[str]] = {}
    for needle in _needles(secret):
        locations = _durable_locations(db_path, needle)
        if locations:
            leaked[needle] = locations
    assert leaked == {}, f"secret-shaped fixture reached durable SQLite values: {leaked}"


def _table_counts(db_path: Path, *tables: str) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        return {
            table: int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
                ).fetchone()[0]
            )
            for table in tables
        }


def _claim_row(db_path: Path, claim_id: int) -> dict[str, object]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    assert row is not None
    return dict(row)


def _event_row(db_path: Path) -> dict[str, object]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    return dict(row)


def _assert_chain_intact(store: SQLiteStore) -> None:
    report = store.reconcile_integrity(fix=False)
    summary = report["summary"]
    assert summary["hash_chain_issues"] == 0
    assert summary["tenant_hash_chain_issues"] == 0


def _assert_rejected_without_echo(
    operation: Callable[[], object],
    secret: str,
) -> None:
    with pytest.raises(SensitiveMetadataError) as rejected:
        operation()
    assert secret not in str(rejected.value)
    assert _literal_secret() not in str(rejected.value)


def _new_store(tmp_path: Path, name: str) -> tuple[SQLiteStore, Path]:
    db_path = tmp_path / f"{name}.db"
    store = SQLiteStore(db_path)
    store.init_db()
    return store, db_path


def _safe_claim(store: SQLiteStore, suffix: str = "one"):
    return store.create_claim(
        text=f"Safe direct-store claim {suffix}.",
        citations=[CitationInput(source="unit-test", locator=f"case:{suffix}")],
        subject=f"safe-subject-{suffix}",
        predicate="safe-predicate",
        object_value="safe-object",
        scope="project:r14-store-tests",
        source_agent="r14-store-tests",
    )


@pytest.mark.parametrize("encoding", ["literal", "base64"])
def test_direct_create_sanitizes_all_claim_content_without_mutating_input(
    tmp_path: Path,
    encoding: str,
) -> None:
    store, db_path = _new_store(tmp_path, f"create-content-{encoding}")
    secret = _secret(encoding)
    citations = [CitationInput(source="unit-test", locator="create-content")]
    citations_before = [asdict(citation) for citation in citations]

    claim = store.create_claim(
        text=secret,
        citations=citations,
        subject=f"subject {secret}",
        predicate=f"predicate {secret}",
        object_value=f"object {secret}",
        scope="project:r14-store-tests",
        source_agent="r14-store-tests",
    )

    assert [asdict(citation) for citation in citations] == citations_before
    for value in (claim.text, claim.subject, claim.predicate, claim.object_value):
        assert value is not None
        assert all(needle not in value for needle in _needles(secret))
        assert "[REDACTED:" in value
    _assert_absent_everywhere(db_path, secret)


@pytest.mark.parametrize("encoding", ["literal", "base64"])
def test_direct_create_rejects_sensitive_identifier_before_any_row(
    tmp_path: Path,
    encoding: str,
) -> None:
    store, db_path = _new_store(tmp_path, f"create-metadata-{encoding}")
    secret = _secret(encoding)

    _assert_rejected_without_echo(
        lambda: store.create_claim(
            text="Safe content for metadata rejection.",
            citations=[CitationInput(source="unit-test")],
            idempotency_key=secret,
            scope="project:r14-store-tests",
            source_agent="r14-store-tests",
        ),
        secret,
    )

    assert _table_counts(db_path, "claims", "citations", "events") == {
        "claims": 0,
        "citations": 0,
        "events": 0,
    }
    _assert_absent_everywhere(db_path, secret)


@pytest.mark.parametrize(
    ("field", "encoding"),
    [("source", "literal"), ("locator", "base64")],
)
def test_direct_create_rejects_sensitive_citation_metadata_before_any_row(
    tmp_path: Path,
    field: str,
    encoding: str,
) -> None:
    store, db_path = _new_store(tmp_path, f"citation-{field}-{encoding}")
    secret = _secret(encoding)
    citation = CitationInput(
        source=secret if field == "source" else "unit-test",
        locator=secret if field == "locator" else "case:citation-metadata",
    )
    before = asdict(citation)

    _assert_rejected_without_echo(
        lambda: store.create_claim(
            text="Safe citation metadata rejection case.",
            citations=[citation],
            scope="project:r14-store-tests",
            source_agent="r14-store-tests",
        ),
        secret,
    )

    assert asdict(citation) == before
    assert _table_counts(db_path, "claims", "citations", "events") == {
        "claims": 0,
        "citations": 0,
        "events": 0,
    }
    _assert_absent_everywhere(db_path, secret)


@pytest.mark.parametrize("encoding", ["literal", "base64"])
def test_direct_create_sanitizes_citation_excerpt_without_mutating_input(
    tmp_path: Path,
    encoding: str,
) -> None:
    store, db_path = _new_store(tmp_path, f"citation-excerpt-{encoding}")
    secret = _secret(encoding)
    citation = CitationInput(
        source="unit-test",
        locator="case:citation-excerpt",
        excerpt=f"quoted evidence {secret}",
    )
    before = asdict(citation)

    claim = store.create_claim(
        text="Safe claim with sensitive citation content.",
        citations=[citation],
        scope="project:r14-store-tests",
        source_agent="r14-store-tests",
    )

    assert asdict(citation) == before
    assert claim.citations[0].excerpt is not None
    assert "[REDACTED:" in claim.citations[0].excerpt
    _assert_absent_everywhere(db_path, secret)


@pytest.mark.parametrize(
    ("field", "encoding"),
    [("tenant_id", "literal"), ("source_agent", "base64")],
)
def test_direct_create_rejects_sensitive_effective_identity(
    tmp_path: Path,
    field: str,
    encoding: str,
) -> None:
    store, db_path = _new_store(tmp_path, f"effective-identity-{field}")
    secret = _secret(encoding)
    kwargs: dict[str, object] = {
        "tenant_id": "tenant-safe",
        "visibility": "private",
        "source_agent": "principal-safe",
    }
    kwargs[field] = secret

    _assert_rejected_without_echo(
        lambda: store.create_claim(
            text="Safe effective identity rejection case.",
            citations=[CitationInput(source="unit-test")],
            scope="project:r14-store-tests",
            **kwargs,
        ),
        secret,
    )

    assert _table_counts(db_path, "claims", "citations", "events") == {
        "claims": 0,
        "citations": 0,
        "events": 0,
    }
    _assert_absent_everywhere(db_path, secret)


@pytest.mark.parametrize("encoding", ["literal", "base64"])
def test_update_claim_structure_sanitizes_content_fields(
    tmp_path: Path,
    encoding: str,
) -> None:
    store, db_path = _new_store(tmp_path, f"update-structure-content-{encoding}")
    claim = store.create_claim(
        text="Safe unstructured claim for structure enrichment.",
        citations=[CitationInput(source="unit-test", locator="update-structure")],
        scope="project:r14-store-tests",
        source_agent="r14-store-tests",
    )
    secret = _secret(encoding)

    store.update_claim_structure(
        claim.id,
        subject=f"subject {secret}",
        predicate=f"predicate {secret}",
        object_value=f"object {secret}",
    )

    updated = store.get_claim(claim.id, include_citations=False)
    assert updated is not None
    for value in (updated.subject, updated.predicate, updated.object_value):
        assert value is not None
        assert all(needle not in value for needle in _needles(secret))
        assert "[REDACTED:" in value
    _assert_absent_everywhere(db_path, secret)


def test_update_claim_structure_rejects_metadata_without_partial_update(
    tmp_path: Path,
) -> None:
    store, db_path = _new_store(tmp_path, "update-structure-metadata")
    claim = _safe_claim(store)
    secret = _secret("base64")
    before_row = _claim_row(db_path, claim.id)
    before_counts = _table_counts(db_path, "claims", "citations", "events")

    _assert_rejected_without_echo(
        lambda: store.update_claim_structure(
            claim.id,
            claim_type=secret,
            subject="must-not-partially-update",
        ),
        secret,
    )

    assert _claim_row(db_path, claim.id) == before_row
    assert _table_counts(db_path, "claims", "citations", "events") == before_counts
    _assert_absent_everywhere(db_path, secret)
    _assert_chain_intact(store)


@pytest.mark.parametrize("encoding", ["literal", "base64"])
def test_set_normalized_text_sanitizes_claim_and_fts_copy(
    tmp_path: Path,
    encoding: str,
) -> None:
    store, db_path = _new_store(tmp_path, f"normalized-single-{encoding}")
    claim = _safe_claim(store)
    secret = _secret(encoding)

    store.set_normalized_text(claim.id, f"normalized {secret}")

    with store.connect() as conn:
        normalized = conn.execute(
            "SELECT normalized_text FROM claims WHERE id = ?",
            (claim.id,),
        ).fetchone()[0]
        indexed = conn.execute(
            "SELECT normalized_text FROM claims_fts WHERE rowid = ?",
            (claim.id,),
        ).fetchone()[0]
    for value in (str(normalized), str(indexed)):
        assert all(needle not in value for needle in _needles(secret))
        assert "[REDACTED:" in value
    _assert_absent_everywhere(db_path, secret)


def test_set_normalized_texts_batch_sanitizes_fts_and_preserves_mapping(
    tmp_path: Path,
) -> None:
    store, db_path = _new_store(tmp_path, "normalized-batch")
    first = _safe_claim(store, "first")
    second = _safe_claim(store, "second")
    literal = _secret("literal")
    encoded = _secret("base64")
    updates = {
        first.id: f"first normalized {literal}",
        second.id: f"second normalized {encoded}",
    }
    before = copy.deepcopy(updates)

    store.set_normalized_texts_batch(updates)

    assert updates == before
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT c.id, c.normalized_text, f.normalized_text "
            "FROM claims AS c JOIN claims_fts AS f ON f.rowid = c.id "
            "WHERE c.id IN (?, ?) ORDER BY c.id",
            (first.id, second.id),
        ).fetchall()
    assert len(rows) == 2
    _assert_absent_everywhere(db_path, literal)
    _assert_absent_everywhere(db_path, encoded)


@pytest.mark.parametrize("encoding", ["literal", "base64"])
def test_record_event_sanitizes_details_and_deep_values_immutably(
    tmp_path: Path,
    encoding: str,
) -> None:
    store, db_path = _new_store(tmp_path, f"event-content-{encoding}")
    secret = _secret(encoding)
    payload = {"outer": [{"middle": {"value": f"payload {secret}"}}]}
    before = copy.deepcopy(payload)

    store.record_event(
        claim_id=None,
        event_type="system",
        details=f"event detail {secret}",
        payload=payload,
    )

    assert payload == before
    row = _event_row(db_path)
    assert "[REDACTED:" in str(row["details"])
    persisted = json.loads(str(row["payload_json"]))
    assert "[REDACTED:" in persisted["outer"][0]["middle"]["value"]
    _assert_absent_everywhere(db_path, secret)
    _assert_chain_intact(store)


@pytest.mark.parametrize("encoding", ["literal", "base64"])
def test_record_event_rejects_sensitive_json_key_without_appending(
    tmp_path: Path,
    encoding: str,
) -> None:
    store, db_path = _new_store(tmp_path, f"event-key-{encoding}")
    secret = _secret(encoding)
    payload = {"outer": [{secret: "benign value"}]}
    before = copy.deepcopy(payload)

    _assert_rejected_without_echo(
        lambda: store.record_event(
            claim_id=None,
            event_type="system",
            details="safe event key rejection",
            payload=payload,
        ),
        secret,
    )

    assert payload == before
    assert _table_counts(db_path, "events") == {"events": 0}
    _assert_absent_everywhere(db_path, secret)
    _assert_chain_intact(store)


def test_record_event_sanitizes_structured_key_value_secret(
    tmp_path: Path,
) -> None:
    store, db_path = _new_store(tmp_path, "event-structured-value")
    credential = "R14StorePass9!xYz7LongFixture"
    assert scan_text_for_findings(credential) == []
    assert "password_assignment" in scan_text_for_findings(f"password={credential}")

    store.record_event(
        claim_id=None,
        event_type="system",
        details="safe structured payload case",
        payload={"password": credential},
    )

    row = _event_row(db_path)
    persisted = json.loads(str(row["payload_json"]))
    assert persisted["password"] == "[REDACTED:structured_secret]"
    assert _durable_locations(db_path, credential) == []
    _assert_chain_intact(store)


@pytest.mark.parametrize(
    "nested_value",
    [
        ["R14StorePass9!xYz7LongFixture"],
        {"value": "R14StorePass9!xYz7LongFixture"},
    ],
)
def test_record_event_propagates_structured_secret_context_into_containers(
    tmp_path: Path,
    nested_value: object,
) -> None:
    store, db_path = _new_store(tmp_path, "event-structured-container")
    credential = "R14StorePass9!xYz7LongFixture"
    payload = {"password": nested_value}

    store.record_event(
        claim_id=None,
        event_type="system",
        details="safe structured container case",
        payload=payload,
    )

    row = _event_row(db_path)
    persisted = str(row["payload_json"])
    assert credential not in persisted
    assert "[REDACTED:structured_secret]" in persisted
    assert _durable_locations(db_path, credential) == []
    _assert_chain_intact(store)


@pytest.mark.parametrize(
    "nested_value",
    [123456789, [123456789], {"value": 123456789}],
)
def test_record_event_sanitizes_numeric_structured_credentials(
    tmp_path: Path,
    nested_value: object,
) -> None:
    store, db_path = _new_store(tmp_path, "event-structured-number")
    payload = {"password": nested_value}

    store.record_event(
        claim_id=None,
        event_type="system",
        details="safe structured numeric case",
        payload=payload,
    )

    persisted = str(_event_row(db_path)["payload_json"])
    assert "123456789" not in persisted
    assert "[REDACTED:structured_secret]" in persisted
    assert _durable_locations(db_path, "123456789") == []
    _assert_chain_intact(store)


def test_record_event_rejects_sensitive_event_type_without_echo(
    tmp_path: Path,
) -> None:
    store, db_path = _new_store(tmp_path, "event-type")
    secret = _secret("base64")

    _assert_rejected_without_echo(
        lambda: store.record_event(
            claim_id=None,
            event_type=secret,
            details="safe event type rejection",
        ),
        secret,
    )

    assert _table_counts(db_path, "events") == {"events": 0}
    _assert_absent_everywhere(db_path, secret)


def test_internal_event_insert_sanitizes_before_hashing_and_preserves_chain(
    tmp_path: Path,
) -> None:
    store, db_path = _new_store(tmp_path, "internal-event-content")
    literal = _secret("literal")
    encoded = _secret("base64")
    payload_json = json.dumps({"outer": [{"value": f"payload {encoded}"}]})

    with store.connect() as conn:
        store._insert_event_row(
            conn,
            claim_id=None,
            event_type="system",
            from_status=None,
            to_status=None,
            details=f"internal detail {literal}",
            payload_json=payload_json,
            created_at="2026-07-11T00:00:00+00:00",
        )
        conn.commit()

    row = _event_row(db_path)
    assert "[REDACTED:" in str(row["details"])
    persisted = json.loads(str(row["payload_json"]))
    assert "[REDACTED:" in persisted["outer"][0]["value"]
    _assert_absent_everywhere(db_path, literal)
    _assert_absent_everywhere(db_path, encoded)
    _assert_chain_intact(store)


def test_internal_event_insert_rejects_sensitive_json_key_before_append(
    tmp_path: Path,
) -> None:
    store, db_path = _new_store(tmp_path, "internal-event-key")
    secret = _secret("base64")
    payload_json = json.dumps({"outer": [{secret: "benign value"}]})

    def insert() -> None:
        with store.connect() as conn:
            store._insert_event_row(
                conn,
                claim_id=None,
                event_type="system",
                from_status=None,
                to_status=None,
                details="safe internal event key rejection",
                payload_json=payload_json,
                created_at="2026-07-11T00:00:00+00:00",
            )
            conn.commit()

    _assert_rejected_without_echo(insert, secret)
    assert _table_counts(db_path, "events") == {"events": 0}
    _assert_absent_everywhere(db_path, secret)
    _assert_chain_intact(store)


@pytest.mark.parametrize(
    ("field", "encoding"),
    [("tenant_id", "literal"), ("principal", "base64")],
)
def test_postgres_rejects_sensitive_bound_identity_before_connect(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    encoding: str,
) -> None:
    secret = _secret(encoding)
    identity = {"tenant_id": "tenant-safe", "principal": "principal-safe"}
    identity[field] = secret
    store = PostgresStore(
        "postgresql://unused.invalid/memorymaster",
        tenant_id=identity["tenant_id"],
        require_tenant=True,
        principal=identity["principal"],
        allowed_scopes=["project:r14-store-tests"],
    )
    connect_calls = 0

    def unexpected_connect():
        nonlocal connect_calls
        connect_calls += 1
        raise AssertionError("Postgres connection attempted before envelope rejection")

    monkeypatch.setattr(store, "connect", unexpected_connect)

    _assert_rejected_without_echo(
        lambda: store.create_claim(
            text="Safe hermetic Postgres identity rejection case.",
            citations=[CitationInput(source="unit-test")],
            scope="project:r14-store-tests",
            visibility="private",
            source_agent="principal-safe",
        ),
        secret,
    )
    assert connect_calls == 0


def test_postgres_rejects_direct_claim_metadata_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = _secret("base64")
    store = PostgresStore("postgresql://unused.invalid/memorymaster")
    connect_calls = 0

    def unexpected_connect():
        nonlocal connect_calls
        connect_calls += 1
        raise AssertionError("Postgres connection attempted before envelope rejection")

    monkeypatch.setattr(store, "connect", unexpected_connect)
    _assert_rejected_without_echo(
        lambda: store.create_claim(
            text="Safe hermetic Postgres metadata rejection case.",
            citations=[CitationInput(source="unit-test")],
            idempotency_key=secret,
        ),
        secret,
    )
    assert connect_calls == 0


def test_postgres_event_rejects_sensitive_json_key_before_driver_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = _secret("base64")
    store = PostgresStore("postgresql://unused.invalid/memorymaster")
    driver_loads = 0

    def unexpected_driver_load():
        nonlocal driver_loads
        driver_loads += 1
        raise AssertionError("Postgres driver loaded before event envelope rejection")

    monkeypatch.setattr(store, "_load_psycopg", unexpected_driver_load)
    _assert_rejected_without_echo(
        lambda: store._insert_event_row(
            None,
            claim_id=None,
            event_type="system",
            from_status=None,
            to_status=None,
            details="safe hermetic event key rejection",
            payload={secret: "benign value"},
            created_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
        ),
        secret,
    )
    assert driver_loads == 0


@pytest.mark.parametrize("field", ["tenant_id", "principal", "allowed_scopes"])
def test_postgres_record_event_rejects_bound_identity_before_connect(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    secret = _secret("base64")
    identity: dict[str, object] = {
        "tenant_id": "tenant-safe",
        "principal": "principal-safe",
        "allowed_scopes": ["project:r14-store-tests"],
    }
    identity[field] = [secret] if field == "allowed_scopes" else secret
    store = PostgresStore(
        "postgresql://unused.invalid/memorymaster",
        tenant_id=identity["tenant_id"],
        require_tenant=True,
        principal=identity["principal"],
        allowed_scopes=identity["allowed_scopes"],
    )
    connect_calls = 0

    def unexpected_connect():
        nonlocal connect_calls
        connect_calls += 1
        raise AssertionError("Postgres connection attempted before bound identity rejection")

    monkeypatch.setattr(store, "connect", unexpected_connect)
    _assert_rejected_without_echo(
        lambda: store.record_event(
            claim_id=None,
            event_type="system",
            details="safe bound identity rejection",
        ),
        secret,
    )
    assert connect_calls == 0


@pytest.mark.parametrize(
    "operation_name",
    ["get_claim", "set_normalized_text", "set_normalized_texts_batch", "update_claim_structure"],
)
def test_postgres_connect_rejects_bound_identity_before_network_for_all_paths(
    monkeypatch: pytest.MonkeyPatch,
    operation_name: str,
) -> None:
    secret = _secret("base64")
    store = PostgresStore(
        "postgresql://unused.invalid/memorymaster",
        tenant_id="tenant-safe",
        require_tenant=True,
        principal=secret,
        allowed_scopes=["project:r14-store-tests"],
    )
    open_calls = 0

    def unexpected_open():
        nonlocal open_calls
        open_calls += 1
        raise AssertionError("Postgres network opened before bound identity rejection")

    monkeypatch.setattr(store, "_open_connection", unexpected_open)
    operations: dict[str, Callable[[], object]] = {
        "get_claim": lambda: store.get_claim(1),
        "set_normalized_text": lambda: store.set_normalized_text(1, "safe normalized text"),
        "set_normalized_texts_batch": lambda: store.set_normalized_texts_batch(
            {1: "safe normalized text"}
        ),
        "update_claim_structure": lambda: store.update_claim_structure(
            1,
            subject="safe subject",
        ),
    }

    _assert_rejected_without_echo(operations[operation_name], secret)
    assert open_calls == 0
