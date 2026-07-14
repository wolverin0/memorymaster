from __future__ import annotations

import base64
import copy
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from memorymaster.bridges import atlas_llm_extractor
from memorymaster.bridges.action_exporters import export_approved_actions
from memorymaster.bridges.action_extractor import propose_actions_from_evidence
from memorymaster.bridges.atlas_llm_extractor import extract_atlas_claims_llm
from memorymaster.core.service import MemoryService
from memorymaster.stores import _storage_sources
from memorymaster.stores._storage_sources import _SourceItemsMixin
from memorymaster.stores.postgres_store import PostgresStore


LITERAL = "OPENAI_API_KEY=sk-proj-FAKEatlasPersistedEnvelope1234567890ABCD"
ENCODED = base64.b64encode(LITERAL.encode()).decode()


@pytest.fixture
def service(tmp_path: Path) -> MemoryService:
    value = MemoryService(tmp_path / "atlas-envelope.db", workspace_root=tmp_path)
    value.init_db()
    return value


def _seed_parent(service: MemoryService):
    source = service.upsert_external_source(
        source_type="whatsapp", display_name="primary", config_json={"mode": "safe"}
    )
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id="message-1",
        item_type="message",
        text="safe source text",
    )
    return source, item


@pytest.mark.parametrize("secret", [LITERAL, ENCODED])
def test_atlas_content_and_nested_json_are_sanitized_without_mutating_input(
    service: MemoryService, secret: str
) -> None:
    config = {"outer": [{"credential": secret}], "safe": "kept"}
    original = copy.deepcopy(config)
    source = service.upsert_external_source(
        source_type="whatsapp", display_name="primary", config_json=config
    )
    assert config == original
    assert secret not in (source.config_json or "")
    assert json.loads(source.config_json or "{}")["safe"] == "kept"

    payload = {"outer": [{"credential": secret}], "safe": "kept"}
    original = copy.deepcopy(payload)
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id="message-1",
        item_type="message",
        sender_name=f"name {secret}",
        text=f"body {secret}",
        payload_json=payload,
    )
    assert payload == original
    assert secret not in repr(item)

    evidence = service.add_evidence_item(
        source_item_id=item.id,
        evidence_type="message_text",
        text=f"evidence {secret}",
        media_path=f"media/{secret}",
        provider=f"provider {secret}",
        payload_json=payload,
    )
    assert secret not in repr(evidence)

    proposal = service.create_action_proposal(
        proposal_type="task",
        title=f"title {secret}",
        description=f"description {secret}",
        source_item_id=item.id,
        evidence_item_id=evidence.id,
        destination="manual",
        payload_json=payload,
    )
    assert secret not in repr(proposal)

    retry = service.enqueue_media_retry(
        source_item_id=item.id,
        media_key="safe-key",
        chat_id=f"chat {secret}",
        media_type=f"audio {secret}",
        media_path=f"media/{secret}",
        media_url=f"https://example.invalid/file?token={secret}",
    )
    retry = service.record_media_retry_outcome(
        retry.id, status="failed", last_error=f"failure {secret}"
    )
    assert secret not in repr(retry)


def test_unsafe_legacy_rows_are_hidden_from_all_atlas_read_and_derivation_surfaces(
    service: MemoryService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, item = _seed_parent(service)
    with service.store.connect() as conn:
        conn.execute("UPDATE source_items SET text = ? WHERE id = ?", (ENCODED, item.id))
        evidence_id = conn.execute(
            "INSERT INTO evidence_items (source_item_id,evidence_type,text,created_at) VALUES (?,?,?,?)",
            (item.id, "message_text", ENCODED, "2026-01-01T00:00:00Z"),
        ).lastrowid
        proposal_id = conn.execute(
            """INSERT INTO action_proposals
               (proposal_type,title,destination,status,confidence,payload_json,created_at,updated_at)
               VALUES ('task',?,'super-productivity','approved',0.5,?, ?, ?)""",
            (ENCODED, json.dumps({"nested": ENCODED}), "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        ).lastrowid
        conn.execute(
            """INSERT INTO media_retry_queue
               (source_item_id,media_key,status,attempt_count,last_error,created_at,updated_at)
               VALUES (?,?, 'pending',0,?,?,?)""",
            (item.id, "legacy-safe-key", ENCODED, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        conn.commit()

    assert service.get_source_item(source_id=source.id, source_item_id="message-1") is None
    assert service.get_source_item_by_id(item.id) is None
    assert all(row.id != evidence_id for row in service.list_evidence_items())
    assert all(row.id != proposal_id for row in service.list_action_proposals())
    assert service.list_media_retries() == []
    assert service.claim_pending_media_retries() == []

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        atlas_llm_extractor,
        "call_llm",
        lambda prompt, text, **kwargs: calls.append((prompt, text)) or "[]",
    )
    assert extract_atlas_claims_llm(service, scope="project:test").scanned == 0
    assert calls == []
    assert propose_actions_from_evidence(service).scanned == 0

    output = tmp_path / "actions.json"
    assert export_approved_actions(service, output).exported == 0
    rendered = output.read_text(encoding="utf-8")
    assert ENCODED not in rendered


def test_safe_atlas_round_trip_is_unchanged(service: MemoryService) -> None:
    source, item = _seed_parent(service)
    assert source.display_name == "primary"
    assert service.get_source_item(source_id=source.id, source_item_id="message-1") == item
    evidence = service.add_evidence_item(
        source_item_id=item.id,
        evidence_type="message_text",
        text="Please send the ordinary invoice tomorrow.",
        payload_json={"kind": "text", "parts": ["one", "two"]},
    )
    assert service.list_evidence_items() == [evidence]


@pytest.mark.parametrize(
    ("family", "field"),
    [
        ("external", "source_type"), ("external", "display_name"),
        ("source", "source_item_id"), ("source", "item_type"),
        ("source", "occurred_at"), ("source", "content_hash"),
        ("evidence", "evidence_type"),
        ("action_create", "proposal_type"), ("action_create", "destination"),
        ("action_create", "idempotency_key"), ("action_create", "suggested_due_at"),
        ("action_status", "exported_at"), ("action_fields", "suggested_due_at"),
        ("retry_create", "media_key"), ("retry_create", "next_attempt_time"),
        ("retry_outcome", "next_attempt_time"),
    ],
)
def test_atlas_metadata_write_paths_reject_before_sql(
    service: MemoryService, family: str, field: str
) -> None:
    source, item = _seed_parent(service)
    service.add_evidence_item(source_item_id=item.id, evidence_type="message_text")
    proposal = service.create_action_proposal(proposal_type="task", title="safe")
    retry = service.enqueue_media_retry(source_item_id=item.id, media_key="safe-key")
    operations = {
        "external": lambda: service.upsert_external_source(
            **{"source_type": "whatsapp", "display_name": "secondary", field: ENCODED}
        ),
        "source": lambda: service.upsert_source_item(
            **{"source_id": source.id, "source_item_id": "message-2", "item_type": "message", field: ENCODED}
        ),
        "evidence": lambda: service.add_evidence_item(
            **{"source_item_id": item.id, "evidence_type": "message_text", field: ENCODED}
        ),
        "action_create": lambda: service.create_action_proposal(
            **{"proposal_type": "task", "title": "safe", field: ENCODED}
        ),
        "action_status": lambda: service.update_action_proposal_status(
            proposal.id, **{"status": "exported", field: ENCODED}
        ),
        "action_fields": lambda: service.update_action_proposal_fields(
            proposal.id, **{field: ENCODED}
        ),
        "retry_create": lambda: service.enqueue_media_retry(
            **{"source_item_id": item.id, "media_key": "new-key", field: ENCODED}
        ),
        "retry_outcome": lambda: service.record_media_retry_outcome(
            retry.id, **{"status": "failed", field: ENCODED}
        ),
    }
    with service.store.connect() as conn:
        before = tuple(
            int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("external_sources", "source_items", "evidence_items", "action_proposals", "media_retry_queue", "events")
        )
    with pytest.raises(ValueError):
        operations[family]()
    with service.store.connect() as conn:
        after = tuple(
            int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("external_sources", "source_items", "evidence_items", "action_proposals", "media_retry_queue", "events")
        )
    assert after == before


def test_action_status_rejects_encoded_secret_in_exported_at_before_sql(
    service: MemoryService,
) -> None:
    _, item = _seed_parent(service)
    proposal = service.create_action_proposal(
        proposal_type="task", title="safe", source_item_id=item.id
    )
    with pytest.raises(ValueError):
        service.update_action_proposal_status(
            proposal.id, status="exported", exported_at=ENCODED
        )
    current = service.list_action_proposals(limit=1)[0]
    assert current.status == "candidate"
    assert current.exported_at is None


def test_action_fields_rejects_encoded_secret_in_suggested_due_at_before_sql(
    service: MemoryService,
) -> None:
    _, item = _seed_parent(service)
    proposal = service.create_action_proposal(
        proposal_type="task", title="safe", source_item_id=item.id
    )
    with pytest.raises(ValueError):
        service.update_action_proposal_fields(
            proposal.id, suggested_due_at=ENCODED
        )
    current = service.list_action_proposals(limit=1)[0]
    assert current.suggested_due_at is None


def test_list_limits_count_safe_rows_after_arbitrary_unsafe_prefix(
    service: MemoryService,
) -> None:
    _, item = _seed_parent(service)
    with service.store.connect() as conn:
        for index in range(251):
            conn.execute(
                "INSERT INTO evidence_items (source_item_id,evidence_type,text,created_at) VALUES (?,?,?,?)",
                (item.id, "message_text", ENCODED, "2026-01-01T00:00:00Z"),
            )
        safe_evidence_ids = [
            int(
                conn.execute(
                    "INSERT INTO evidence_items (source_item_id,evidence_type,text,created_at) VALUES (?,?,?,?)",
                    (item.id, "message_text", f"safe-{index}", "2026-01-02T00:00:00Z"),
                ).lastrowid
            )
            for index in range(2)
        ]
        for index in range(251):
            conn.execute(
                """INSERT INTO media_retry_queue
                   (source_item_id,media_key,status,attempt_count,last_error,created_at,updated_at)
                   VALUES (?,?,'failed',0,?,?,?)""",
                (item.id, f"unsafe-{index}", ENCODED, "2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z"),
            )
        safe_retry_ids = [
            int(
                conn.execute(
                    """INSERT INTO media_retry_queue
                       (source_item_id,media_key,status,attempt_count,created_at,updated_at)
                       VALUES (?,?,'failed',0,?,?)""",
                    (item.id, f"safe-{index}", "2026-01-01T00:00:00Z", f"2026-01-02T00:00:0{index}Z"),
                ).lastrowid
            )
            for index in range(2)
        ]
        for index in range(251):
            conn.execute(
                """INSERT INTO action_proposals
                   (proposal_type,title,destination,status,confidence,created_at,updated_at)
                   VALUES ('task',?,'manual','candidate',0.5,?,?)""",
                (ENCODED, "2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z"),
            )
        safe_action_ids = [
            int(
                conn.execute(
                    """INSERT INTO action_proposals
                       (proposal_type,title,destination,status,confidence,created_at,updated_at)
                       VALUES ('task',?,'manual','candidate',0.5,?,?)""",
                    (f"safe-{index}", "2026-01-01T00:00:00Z", f"2026-01-02T00:00:0{index}Z"),
                ).lastrowid
            )
            for index in range(2)
        ]
        conn.commit()

    assert [row.id for row in service.list_evidence_items(limit=2)] == safe_evidence_ids
    assert [row.id for row in service.list_media_retries(status="failed", limit=2)] == list(
        reversed(safe_retry_ids)
    )
    assert [row.id for row in service.list_action_proposals(limit=2)] == list(
        reversed(safe_action_ids)
    )


def test_sqlite_claim_skips_unsafe_prefix_without_mutation_or_events(
    service: MemoryService,
) -> None:
    _, item = _seed_parent(service)
    with service.store.connect() as conn:
        unsafe_ids = [
            int(
                conn.execute(
                    """INSERT INTO media_retry_queue
                       (source_item_id,media_key,status,attempt_count,last_error,created_at,updated_at)
                       VALUES (?,?,'pending',0,?,?,?)""",
                    (item.id, f"unsafe-{index}", ENCODED, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
                ).lastrowid
            )
            for index in range(251)
        ]
        safe_ids = [
            int(
                conn.execute(
                    """INSERT INTO media_retry_queue
                       (source_item_id,media_key,status,attempt_count,created_at,updated_at)
                       VALUES (?,?,'pending',0,?,?)""",
                    (item.id, f"safe-{index}", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
                ).lastrowid
            )
            for index in range(2)
        ]
        before_events = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        conn.commit()

    assert [row.id for row in service.claim_pending_media_retries(limit=2)] == safe_ids
    with service.store.connect() as conn:
        unsafe = conn.execute(
            f"SELECT status,attempt_count FROM media_retry_queue WHERE id IN ({','.join('?' for _ in unsafe_ids)}) ORDER BY id",
            unsafe_ids,
        ).fetchall()
        after_events = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    assert [(row["status"], row["attempt_count"]) for row in unsafe] == [
        ("pending", 0)
    ] * len(unsafe_ids)
    assert after_events - before_events == len(safe_ids)


@pytest.mark.parametrize("field", ["config_json", "created_at"])
def test_sqlite_external_source_upsert_rejects_unsafe_existing_row_before_mutation(
    service: MemoryService, field: str
) -> None:
    source, _ = _seed_parent(service)
    with service.store.connect() as conn:
        conn.execute(f"UPDATE external_sources SET {field} = ? WHERE id = ?", (ENCODED, source.id))
        conn.commit()
    with service.store.connect() as conn:
        before = dict(conn.execute("SELECT * FROM external_sources WHERE id = ?", (source.id,)).fetchone())
    with pytest.raises(ValueError, match="unsafe"):
        service.upsert_external_source(
            source_type="whatsapp", display_name="primary", config_json={"mode": "changed"}
        )
    with service.store.connect() as conn:
        after = dict(conn.execute("SELECT * FROM external_sources WHERE id = ?", (source.id,)).fetchone())
    assert after == before


@pytest.mark.parametrize("field", ["created_at", "sensitivity"])
def test_sqlite_source_item_upsert_rejects_unsafe_existing_row_before_mutation_or_event(
    service: MemoryService, field: str
) -> None:
    source, item = _seed_parent(service)
    with service.store.connect() as conn:
        if field == "sensitivity":
            conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(f"UPDATE source_items SET {field} = ? WHERE id = ?", (ENCODED, item.id))
        before = dict(conn.execute("SELECT * FROM source_items WHERE id = ?", (item.id,)).fetchone())
        events = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        conn.commit()
    with pytest.raises(ValueError, match="unsafe"):
        service.upsert_source_item(
            source_id=source.id, source_item_id="message-1", item_type="message", text="changed"
        )
    with service.store.connect() as conn:
        after = dict(conn.execute("SELECT * FROM source_items WHERE id = ?", (item.id,)).fetchone())
        after_events = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    assert after == before
    assert after_events == events


@pytest.mark.parametrize("family", ["external", "source"])
def test_upsert_postwrite_safety_failure_rolls_back_atomically(
    service: MemoryService,
    family: str,
) -> None:
    source, item = _seed_parent(service)
    table = "external_sources" if family == "external" else "source_items"
    row_id = source.id if family == "external" else item.id
    with service.store.connect() as conn:
        before = dict(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone())
        conn.execute(
            f"""CREATE TRIGGER poison_{table}_after_update
                AFTER UPDATE ON {table}
                BEGIN
                    UPDATE {table} SET created_at = '{ENCODED}' WHERE id = NEW.id;
                END"""
        )
        conn.commit()

    if family == "external":
        def operation() -> object:
            return service.upsert_external_source(
                source_type="whatsapp",
                display_name="primary",
                config_json={"mode": "changed"},
            )
    else:
        def operation() -> object:
            return service.upsert_source_item(
                source_id=source.id,
                source_item_id="message-1",
                item_type="message",
                text="changed",
            )

    with pytest.raises(ValueError, match="unsafe"):
        operation()
    with service.store.connect() as conn:
        after = dict(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone())
    assert after == before


def test_action_updates_advance_updated_at(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, item = _seed_parent(service)
    timestamps = iter(
        [
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:01Z",
            "2026-01-01T00:00:02Z",
        ]
    )
    monkeypatch.setattr(_storage_sources, "utc_now", lambda: next(timestamps))
    proposal = service.create_action_proposal(
        proposal_type="task", title="safe", source_item_id=item.id
    )
    status_updated = service.update_action_proposal_status(
        proposal.id, status="approved"
    )
    fields_updated = service.update_action_proposal_fields(
        proposal.id, title="changed"
    )
    assert status_updated.updated_at > proposal.updated_at
    assert fields_updated.updated_at > status_updated.updated_at


@pytest.mark.parametrize("secret", [LITERAL, ENCODED])
def test_postgres_json_payload_sanitizes_nested_values_without_mutation(
    secret: str,
) -> None:
    payload = {"outer": [{"credential": secret}], "safe": "kept"}
    original = copy.deepcopy(payload)
    sanitized = PostgresStore._json_payload(payload)
    assert payload == original
    assert secret not in repr(sanitized)
    assert sanitized["safe"] == "kept"


def test_postgres_json_payload_sanitizes_json_string_and_literal() -> None:
    encoded_json = json.dumps({"credential": ENCODED, "safe": "kept"})
    assert ENCODED not in repr(PostgresStore._json_payload(encoded_json))
    assert LITERAL not in repr(PostgresStore._json_payload(LITERAL))


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("update_action_proposal_status", {"status": "exported", "exported_at": ENCODED}),
        ("update_action_proposal_fields", {"suggested_due_at": ENCODED}),
    ],
)
def test_postgres_update_metadata_rejects_before_connect(
    monkeypatch: pytest.MonkeyPatch, method: str, kwargs: dict[str, Any]
) -> None:
    store = object.__new__(PostgresStore)
    monkeypatch.setattr(store, "_deny_unsupported_team_surface", lambda _name: None)
    monkeypatch.setattr(store, "_load_psycopg", lambda: (None, None, lambda value: value))
    monkeypatch.setattr(
        store, "connect", lambda: pytest.fail("unsafe metadata reached SQL")
    )
    with pytest.raises(ValueError):
        getattr(store, method)(1, **kwargs)


class _UpsertPreflightCursor:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.executed: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, _params: object) -> None:
        self.executed.append(sql)
        if not sql.lstrip().startswith("SELECT"):
            pytest.fail("unsafe existing row reached mutation SQL")

    def fetchone(self) -> dict[str, Any]:
        return dict(self.row)


@pytest.mark.parametrize(
    ("method", "row", "kwargs"),
    [
        (
            "upsert_external_source",
            {
                "id": 1, "source_type": "whatsapp", "display_name": "primary",
                "config_json": {"credential": ENCODED}, "created_at": "safe", "updated_at": "safe",
            },
            {"source_type": "whatsapp", "display_name": "primary", "config_json": {"mode": "changed"}},
        ),
        (
            "upsert_source_item",
            {
                "id": 1, "source_id": 1, "source_item_id": "message-1", "item_type": "message",
                "chat_id": None, "sender_id": None, "sender_name": None, "occurred_at": None,
                "text": "safe", "payload_json": None, "content_hash": None, "sensitivity": ENCODED,
                "created_at": "safe", "updated_at": "safe",
            },
            {"source_id": 1, "source_item_id": "message-1", "item_type": "message", "text": "changed"},
        ),
    ],
)
def test_postgres_upserts_preflight_full_existing_row_before_mutation(
    monkeypatch: pytest.MonkeyPatch, method: str, row: dict[str, Any], kwargs: dict[str, Any]
) -> None:
    cursor = _UpsertPreflightCursor(row)
    store = object.__new__(PostgresStore)
    monkeypatch.setattr(store, "_deny_unsupported_team_surface", lambda _name: None)
    monkeypatch.setattr(store, "_load_psycopg", lambda: (None, None, lambda value: value))
    monkeypatch.setattr(store, "connect", lambda: _ClaimConnection(cursor))
    monkeypatch.setattr(store, "_insert_event_row", lambda *_args, **_kwargs: pytest.fail("unsafe row emitted event"))
    with pytest.raises(ValueError, match="unsafe"):
        getattr(store, method)(**kwargs)
    assert len(cursor.executed) == 1
    assert "SELECT *" in cursor.executed[0]


@pytest.mark.parametrize(
    "method",
    [
        "list_evidence_items",
        "list_media_retries",
        "list_action_proposals",
        "claim_pending_media_retries",
    ],
)
def test_sqlite_postgres_zero_limit_behavior_parity(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    postgres = object.__new__(PostgresStore)
    monkeypatch.setattr(postgres, "_deny_unsupported_team_surface", lambda _name: None)
    monkeypatch.setattr(postgres, "connect", lambda: pytest.fail("zero limit reached SQL"))
    assert getattr(service.store, method)(limit=0) == []
    assert getattr(postgres, method)(limit=0) == []


class _PagedListCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.result: list[dict[str, Any]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: list[Any]) -> None:
        ascending = "ORDER BY created_at ASC" in sql
        time_key = "created_at" if ascending else "updated_at"
        ordered = sorted(
            self.rows,
            key=lambda row: (row[time_key], row["id"]),
            reverse=not ascending,
        )
        if len(params) > 1:
            cursor_time, cursor_id = params[-4], int(params[-2])
            if ascending:
                ordered = [row for row in ordered if (row[time_key], row["id"]) > (cursor_time, cursor_id)]
            else:
                ordered = [row for row in ordered if (row[time_key], row["id"]) < (cursor_time, cursor_id)]
        self.result = ordered[: int(params[-1])]

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self.result)


def _postgres_list_store(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> PostgresStore:
    store = object.__new__(PostgresStore)
    cursor = _PagedListCursor(rows)
    monkeypatch.setattr(store, "_deny_unsupported_team_surface", lambda _name: None)
    monkeypatch.setattr(store, "connect", lambda: _ClaimConnection(cursor))
    return store


def test_postgres_lists_count_safe_rows_after_unsafe_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    common = {"id": 1, "created_at": "2026-01-03T00:00:00Z", "updated_at": "2026-01-03T00:00:00Z"}
    evidence = [
        {**common, "id": index + 1, "source_item_id": 1, "evidence_type": "text", "text": ENCODED,
         "media_path": None, "provider": None, "confidence": None, "payload_json": None,
         "sensitivity": None,
         "created_at": "2026-01-01T00:00:00Z"}
        for index in range(251)
    ] + [
        {**common, "id": index + 252, "source_item_id": 1, "evidence_type": "text", "text": f"safe-{index}",
         "media_path": None, "provider": None, "confidence": None, "payload_json": None,
         "sensitivity": None,
         "created_at": "2026-01-02T00:00:00Z"}
        for index in range(2)
    ]
    retry_base = {**common, "source_item_id": 1, "chat_id": None, "media_type": None, "media_path": None,
                  "media_url": None, "status": "failed", "attempt_count": 0, "last_http_status": None,
                  "next_attempt_time": None, "lease_owner": None, "lease_expires_at": None}
    retries = [{**retry_base, "id": index + 1, "media_key": f"unsafe-{index}", "last_error": ENCODED,
                "updated_at": "2026-01-03T00:00:00Z"} for index in range(251)] + [
        {**retry_base, "id": index + 252, "media_key": f"safe-{index}", "last_error": None,
         "updated_at": f"2026-01-02T00:00:0{index}Z"} for index in range(2)
    ]
    action_base = {**common, "proposal_type": "task", "description": None, "source_item_id": None,
                   "evidence_item_id": None, "claim_id": None, "suggested_due_at": None, "destination": "manual",
                   "status": "candidate", "confidence": 0.5, "payload_json": None, "exported_at": None,
                   "external_ref": None, "idempotency_key": None}
    actions = [{**action_base, "id": index + 1, "title": ENCODED,
                "updated_at": "2026-01-03T00:00:00Z"} for index in range(251)] + [
        {**action_base, "id": index + 252, "title": f"safe-{index}",
         "updated_at": f"2026-01-02T00:00:0{index}Z"} for index in range(2)
    ]
    assert [row.id for row in _postgres_list_store(monkeypatch, evidence).list_evidence_items(limit=2)] == [252, 253]
    assert [row.id for row in _postgres_list_store(monkeypatch, retries).list_media_retries(limit=2)] == [253, 252]
    assert [row.id for row in _postgres_list_store(monkeypatch, actions).list_action_proposals(limit=2)] == [253, 252]


def test_unsafe_legacy_mutation_and_coalesce_paths_fail_without_changes(
    service: MemoryService,
) -> None:
    source, item = _seed_parent(service)
    evidence = service.add_evidence_item(
        source_item_id=item.id, evidence_type="message_text", text="safe"
    )
    proposal = service.create_action_proposal(
        proposal_type="task", title="safe", evidence_item_id=evidence.id
    )
    retry = service.enqueue_media_retry(source_item_id=item.id, media_key="safe-key")
    with service.store.connect() as conn:
        conn.execute("UPDATE source_items SET text = ? WHERE id = ?", (ENCODED, item.id))
        conn.execute("UPDATE evidence_items SET text = ? WHERE id = ?", (ENCODED, evidence.id))
        conn.execute("UPDATE action_proposals SET description = ? WHERE id = ?", (ENCODED, proposal.id))
        conn.execute("UPDATE media_retry_queue SET last_error = ? WHERE id = ?", (ENCODED, retry.id))
        conn.commit()

    def snapshot(table: str, row_id: int) -> tuple[dict[str, Any], int]:
        with service.store.connect() as conn:
            row = dict(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone())
            event_count = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        return row, event_count

    cases = [
        ("source_items", item.id, lambda: service.set_source_item_sensitivity(item.id, "high")),
        ("evidence_items", evidence.id, lambda: service.set_evidence_item_sensitivity(evidence.id, "high")),
        ("action_proposals", proposal.id, lambda: service.update_action_proposal_status(proposal.id, status="approved")),
        ("action_proposals", proposal.id, lambda: service.update_action_proposal_fields(proposal.id, title="changed")),
        ("media_retry_queue", retry.id, lambda: service.enqueue_media_retry(source_item_id=item.id, media_key="safe-key", chat_id="changed")),
        ("media_retry_queue", retry.id, lambda: service.record_media_retry_outcome(retry.id, status="failed")),
    ]
    for table, row_id, operation in cases:
        before = snapshot(table, row_id)
        with pytest.raises(ValueError, match="unsafe"):
            operation()
        assert snapshot(table, row_id) == before


class _ClaimCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.result: list[dict[str, Any]] = []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.executed.append((sql, params))
        if sql.lstrip().startswith("SELECT"):
            if "status = 'retrying'" in sql:
                self.result = []
                return
            cursor_id = int(params[0])
            self.result = [row for row in self.rows if int(row["id"]) > cursor_id][
                : int(params[-1])
            ]
            return
        safe_ids = set(params[3:])
        self.result = []
        for row in self.rows:
            if row["id"] in safe_ids:
                row.update(
                    status="retrying",
                    attempt_count=row["attempt_count"] + 1,
                    lease_owner=params[0],
                    lease_expires_at=params[1],
                )
                self.result.append(dict(row))

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self.result)


class _ClaimConnection:
    def __init__(self, cursor: _ClaimCursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> _ClaimCursor:
        return self._cursor


def test_postgres_claim_filters_before_update_and_event(monkeypatch: pytest.MonkeyPatch) -> None:
    safe = {
        "id": 252, "source_item_id": 1, "media_key": "safe", "chat_id": None,
        "media_type": None, "media_path": None, "media_url": None, "status": "pending",
        "attempt_count": 0, "last_http_status": None, "last_error": None,
        "next_attempt_time": None, "lease_owner": None, "lease_expires_at": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    unsafe_rows = [
        {**safe, "id": index + 1, "media_key": f"unsafe-{index}", "last_error": ENCODED}
        for index in range(251)
    ]
    cursor = _ClaimCursor([*unsafe_rows, safe])
    store = object.__new__(PostgresStore)
    monkeypatch.setattr(store, "_deny_unsupported_team_surface", lambda _name: None)
    monkeypatch.setattr(store, "connect", lambda: _ClaimConnection(cursor))
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(store, "_insert_event_row", lambda _conn, **kwargs: events.append(kwargs))

    claimed = store.claim_pending_media_retries(limit=1)

    assert [row.id for row in claimed] == [252]
    assert all(row["status"] == "pending" and row["attempt_count"] == 0 for row in unsafe_rows)
    update_params = cursor.executed[-1][1]
    assert update_params[3:] == (252,)
    assert [event["payload"]["retry_id"] for event in events] == [252]
    assert "FOR UPDATE SKIP LOCKED" in cursor.executed[0][0]


def test_sqlite_and_postgres_expose_the_same_atlas_gateway_methods() -> None:
    names = {
        name
        for name, value in inspect.getmembers(_SourceItemsMixin, inspect.isfunction)
        if not name.startswith("_row_to_")
    }
    missing = sorted(name for name in names if not callable(getattr(PostgresStore, name, None)))
    assert missing == []
