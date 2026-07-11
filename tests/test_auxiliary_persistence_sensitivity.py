"""Adversarial tests for auxiliary durable sensitivity boundaries."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memorymaster.core import spool
from memorymaster.core.models import CitationInput
from memorymaster.core.security import (
    SensitiveMetadataError,
    sanitize_persisted_text,
    scan_text_for_findings,
)
from memorymaster.core.service import MemoryService
from memorymaster.govern.feedback import FeedbackTracker
from memorymaster.govern.jobs import spool_drain
from memorymaster.knowledge import rule_miner
from memorymaster.knowledge.daily_notes import (
    export_daily_note_md,
    find_ghost_notes,
    generate_daily_note,
)
from memorymaster.recall import verbatim_store
from memorymaster.recall.verbatim_recall import recall_verbatim
from memorymaster.stores.postgres_store import PostgresStore


def _literal_secret() -> str:
    body = "".join(format((index * 7 + 3) % 16, "x") for index in range(40))
    token = "".join(("gh", "p_", body))
    assert "github_token" in scan_text_for_findings(token)
    return token


def _encoded_secret() -> str:
    token = base64.b64encode(_literal_secret().encode()).decode()
    assert "github_token" in scan_text_for_findings(token)
    return token


def _assert_tree_absent(root: Path, *needles: str) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            raw = path.read_bytes()
            for needle in needles:
                assert needle.encode() not in raw, f"{needle!r} leaked into {path.name}"


def _verbatim_counts(db_path: Path) -> tuple[int, int]:
    with sqlite3.connect(db_path) as conn:
        primary = int(conn.execute("SELECT COUNT(*) FROM verbatim_memories").fetchone()[0])
        indexed = int(conn.execute("SELECT COUNT(*) FROM verbatim_fts").fetchone()[0])
    return primary, indexed


@pytest.mark.parametrize("secret_factory", [_literal_secret, _encoded_secret])
def test_spool_sanitizes_ingest_content_before_first_file_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secret_factory,
) -> None:
    monkeypatch.setenv(spool.ENV_SPOOL_DIR, str(tmp_path / "spool"))
    secret = secret_factory()
    payload = {
        "text": f"Sensitive spooled content {secret}",
        "subject": f"subject {secret}",
        "scope": "project:aux-test",
        "source_agent": "aux-test",
        "citations": [{"source": "unit-test", "excerpt": f"excerpt {secret}"}],
    }
    before = copy.deepcopy(payload)

    path = spool.append(tmp_path / "memory.db", "ingest", payload)

    assert payload == before
    _assert_tree_absent(path.parent, secret, _literal_secret())
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert "[REDACTED:" in json.dumps(envelope["payload"])


@pytest.mark.parametrize(
    ("op", "payload", "idempotency_key"),
    [
        ("ingest", {"text": "safe", "citations": []}, _encoded_secret()),
        (
            "verbatim",
            {
                "session_id": "safe-session",
                "role": "user",
                "content": f"Long enough sensitive turn {_encoded_secret()}",
                "scope": "project:aux-test",
                "source_agent": "aux-test",
            },
            None,
        ),
    ],
)
def test_spool_rejects_sensitive_metadata_and_verbatim_before_file_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    op: str,
    payload: dict[str, object],
    idempotency_key: str | None,
) -> None:
    root = tmp_path / "spool"
    monkeypatch.setenv(spool.ENV_SPOOL_DIR, str(root))

    with pytest.raises(SensitiveMetadataError) as rejected:
        spool.append(
            tmp_path / "memory.db",
            op,
            payload,
            idempotency_key=idempotency_key,
        )

    assert _encoded_secret() not in str(rejected.value)
    assert not root.exists()


def test_quarantine_never_duplicates_sensitive_raw_line_or_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "spool"
    monkeypatch.setenv(spool.ENV_SPOOL_DIR, str(root))
    literal = _literal_secret()
    encoded = _encoded_secret()

    target = spool.quarantine_line(
        tmp_path / "memory.db",
        json.dumps({"payload": {"token": encoded}}),
        f"replay failed with {literal}",
    )

    _assert_tree_absent(root, literal, encoded)
    record = json.loads(target.read_text(encoding="utf-8"))
    assert "[REDACTED:" in json.dumps(record)


def test_drain_validates_boundary_sidecar_before_claim_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(spool.ENV_SPOOL_DIR, str(tmp_path / "spool"))
    db_path = tmp_path / "invalid-sidecar.db"
    service = MemoryService(db_path)
    service.init_db()
    spool_dir = spool.spool_dir_for(db_path)
    spool_dir.mkdir(parents=True)
    envelope = spool.make_envelope(
        "ingest",
        {
            "text": "A safe claim that must not survive invalid audit metadata.",
            "citations": [],
            "scope": "project:aux-test",
            "source_agent": "aux-test",
            "_sanitization": {"findings": ["invalid finding label!"]},
        },
        idempotency_key="invalid-sidecar-order",
    )
    (spool_dir / "999-20260711.jsonl").write_text(
        json.dumps(envelope) + "\n",
        encoding="utf-8",
    )

    result = spool_drain.run(service)

    assert result["drained"] == 0
    assert result["quarantined"] == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0


def test_spooled_redaction_event_failure_rolls_back_claim_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(spool.ENV_SPOOL_DIR, str(tmp_path / "spool"))
    db_path = tmp_path / "atomic-redaction-event.db"
    service = MemoryService(db_path)
    service.init_db()
    original_insert = service.store._insert_event_row

    def _fail_policy_event(conn, **kwargs):
        if (
            kwargs.get("event_type") == "policy_decision"
            and kwargs.get("details") == "sensitive_redaction_applied"
        ):
            raise RuntimeError("synthetic policy event failure")
        return original_insert(conn, **kwargs)

    monkeypatch.setattr(service.store, "_insert_event_row", _fail_policy_event)
    spool.append(
        db_path,
        "ingest",
        {
            "text": f"Sensitive boundary claim {_encoded_secret()}",
            "citations": [],
            "scope": "project:aux-test",
            "source_agent": "aux-test",
        },
        idempotency_key="atomic-redaction-event",
    )

    result = spool_drain.run(service)

    assert result["drained"] == 0
    assert result["quarantined"] == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
        assert conn.execute(
            """SELECT COUNT(*) FROM events
               WHERE details = 'sensitive_redaction_applied'"""
        ).fetchone()[0] == 0


def test_postgres_redaction_event_failure_rolls_back_claim_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Cursor:
        def __init__(self) -> None:
            self.last_sql = ""

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params=None) -> None:
            self.last_sql = " ".join(str(sql).split()).lower()

        def fetchone(self):
            return {"id": 1} if "returning id" in self.last_sql else None

    class _Connection:
        def __init__(self) -> None:
            self.cursor_instance = _Cursor()
            self.committed = False
            self.rolled_back = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.rolled_back = exc_type is not None
            self.committed = exc_type is None
            return False

        def cursor(self):
            return self.cursor_instance

    connection = _Connection()
    store = PostgresStore("postgresql://unused.invalid/memorymaster")
    monkeypatch.setattr(store, "connect", lambda: connection)
    monkeypatch.setattr(store, "_assign_human_id", lambda *args, **kwargs: None)

    def fail_policy_event(conn, **kwargs):
        if kwargs.get("details") == "sensitive_redaction_applied":
            raise RuntimeError("synthetic policy event failure")
        return 1

    monkeypatch.setattr(store, "_insert_event_row", fail_policy_event)

    with pytest.raises(RuntimeError, match="synthetic policy event failure"):
        store.create_claim(
            "A harmless pre-sanitized claim body.",
            [CitationInput(source="test")],
            _pre_sanitization_findings=["github_token"],
        )

    assert connection.rolled_back is True
    assert connection.committed is False


@pytest.mark.parametrize(
    ("field", "secret"),
    [
        ("session_id", _literal_secret()),
        ("role", _encoded_secret()),
        ("content", _encoded_secret()),
        ("scope", _encoded_secret()),
        ("source_agent", _literal_secret()),
        ("timestamp", _literal_secret()),
    ],
)
def test_direct_verbatim_rejects_sensitive_fields_without_primary_or_fts_row(
    tmp_path: Path,
    field: str,
    secret: str,
) -> None:
    db_path = tmp_path / f"verbatim-{field}.db"
    verbatim_store.ensure_verbatim_schema(str(db_path))
    values = {
        "session_id": "safe-session",
        "role": "user",
        "content": "A sufficiently long safe verbatim conversation turn.",
        "scope": "project:aux-test",
        "source_agent": "aux-test",
        "timestamp": "2026-07-11T00:00:00+00:00",
    }
    values[field] = secret

    row_id = verbatim_store.store_verbatim(str(db_path), **values)

    assert row_id is None
    assert _verbatim_counts(db_path) == (0, 0)


def test_verbatim_compatibility_detector_catches_encoded_secret() -> None:
    assert verbatim_store._contains_sensitive(_encoded_secret())


def _seed_verbatim_pair(db_path: Path) -> tuple[int, int]:
    verbatim_store.ensure_verbatim_schema(str(db_path))
    unsafe = f"Auxiliary sentinel legacy row {_encoded_secret()}"
    safe = "Auxiliary sentinel safe neighboring verbatim row"
    with sqlite3.connect(db_path) as conn:
        unsafe_id = int(
            conn.execute(
                """INSERT INTO verbatim_memories
                   (session_id, role, content, scope, timestamp, source_agent)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("legacy", "user", unsafe, "project:aux-test", "2026-07-11T00:00:00Z", "test"),
            ).lastrowid
        )
        safe_id = int(
            conn.execute(
                """INSERT INTO verbatim_memories
                   (session_id, role, content, scope, timestamp, source_agent)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("safe", "user", safe, "project:aux-test", "2026-07-11T00:00:01Z", "test"),
            ).lastrowid
        )
        conn.execute("INSERT INTO verbatim_fts(rowid, content) VALUES (?, ?)", (unsafe_id, unsafe))
        conn.execute("INSERT INTO verbatim_fts(rowid, content) VALUES (?, ?)", (safe_id, safe))
        conn.commit()
    return unsafe_id, safe_id


def test_legacy_sensitive_verbatim_is_hidden_from_both_fts_surfaces(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-verbatim.db"
    unsafe_id, safe_id = _seed_verbatim_pair(db_path)

    direct = verbatim_store.search_verbatim(str(db_path), "auxiliary sentinel", limit=10)
    recalled = recall_verbatim("auxiliary sentinel", "project:aux-test", str(db_path), limit=10)

    assert {row["id"] for row in direct} == {safe_id}
    assert {hit.verbatim_id for hit in recalled} == {safe_id}
    assert unsafe_id not in {row["id"] for row in direct}


def test_verbatim_scope_filter_does_not_match_textual_sibling_scope(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "scope-boundary-verbatim.db"
    verbatim_store.ensure_verbatim_schema(str(db_path))
    rows = (
        ("project:test", "Authorized sibling-boundary verbatim sentinel row."),
        ("project:test-foreign", "Foreign sibling-boundary verbatim sentinel row."),
    )
    with sqlite3.connect(db_path) as conn:
        for scope, content in rows:
            row_id = int(
                conn.execute(
                    """INSERT INTO verbatim_memories
                       (session_id, role, content, scope, timestamp, source_agent)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (scope, "user", content, scope, "2026-07-11T00:00:00Z", "test"),
                ).lastrowid
            )
            conn.execute(
                "INSERT INTO verbatim_fts(rowid, content) VALUES (?, ?)",
                (row_id, content),
            )
        conn.commit()

    direct = verbatim_store.search_verbatim(
        str(db_path),
        "sibling boundary verbatim sentinel",
        scope="project:test",
        limit=10,
    )
    recalled = recall_verbatim(
        "sibling boundary verbatim sentinel",
        "project:test",
        str(db_path),
        limit=10,
    )

    assert {row["scope"] for row in direct} == {"project:test"}
    assert {hit.scope for hit in recalled} == {"project:test"}


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def test_verbatim_qdrant_sync_filters_legacy_secret_before_embedding_and_upsert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "qdrant-verbatim.db"
    unsafe_id, safe_id = _seed_verbatim_pair(db_path)
    requests: list[tuple[str, bytes | None]] = []

    def fake_urlopen(req, timeout):
        requests.append((req.full_url, req.data))
        if req.full_url.endswith(f"/collections/{verbatim_store.QDRANT_COLLECTION}"):
            return _Response({})
        if req.full_url == "https://api.openai.com/v1/embeddings":
            payload = json.loads(req.data.decode())
            return _Response({"data": [{"embedding": [0.0] * verbatim_store.EMBED_DIM} for _ in payload["input"]]})
        return _Response({})

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(verbatim_store, "QDRANT_URL", "http://qdrant.invalid")
    monkeypatch.setattr(verbatim_store.urllib.request, "urlopen", fake_urlopen)

    result = verbatim_store.sync_to_qdrant(str(db_path))

    serialized = b"\n".join(data or b"" for _, data in requests)
    assert _encoded_secret().encode() not in serialized
    point_payloads = [
        json.loads(data.decode())
        for url, data in requests
        if url.endswith("/points") and data is not None
    ]
    point_ids = {
        int(point["id"])
        for payload in point_payloads
        for point in payload["points"]
    }
    assert point_ids == {safe_id}
    assert unsafe_id not in point_ids
    assert result == {"synced": 1, "excluded_sensitive": 1}
    with sqlite3.connect(db_path) as conn:
        states = dict(conn.execute("SELECT id, embedding_synced FROM verbatim_memories"))
    assert states == {unsafe_id: -1, safe_id: 1}


def test_verbatim_qdrant_sync_rejects_embedding_cardinality_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "qdrant-cardinality.db"
    unsafe_id, safe_id = _seed_verbatim_pair(db_path)
    second_safe_id = verbatim_store.store_verbatim(
        str(db_path),
        session_id="second-safe",
        role="user",
        content="A second harmless row must not be marked synced without a vector.",
        scope="project:test",
        source_agent="test",
    )
    requests: list[str] = []

    def fake_urlopen(req, timeout):
        requests.append(req.full_url)
        if req.full_url.endswith(f"/collections/{verbatim_store.QDRANT_COLLECTION}"):
            return _Response({})
        if req.full_url == "https://api.openai.com/v1/embeddings":
            return _Response({"data": [{"embedding": [0.0] * verbatim_store.EMBED_DIM}]})
        pytest.fail("cardinality mismatch reached Qdrant upsert")

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(verbatim_store, "QDRANT_URL", "http://qdrant.invalid")
    monkeypatch.setattr(verbatim_store.urllib.request, "urlopen", fake_urlopen)

    result = verbatim_store.sync_to_qdrant(str(db_path))

    assert result == {"synced": 0, "error": "embedding response cardinality mismatch"}
    assert not any(url.endswith("/points") for url in requests)
    with sqlite3.connect(db_path) as conn:
        states = dict(conn.execute("SELECT id, embedding_synced FROM verbatim_memories"))
    assert states == {unsafe_id: -1, safe_id: 0, second_safe_id: 0}


def test_feedback_scans_full_query_before_truncation_and_direct_write(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "feedback.db"
    service = MemoryService(db_path)
    service.init_db()
    query = ("benign-prefix " * 45) + _encoded_secret()
    tracker = FeedbackTracker(str(db_path))

    assert tracker.record_retrieval([1], query) == 1

    with sqlite3.connect(db_path) as conn:
        persisted = str(conn.execute("SELECT query_text FROM usage_feedback").fetchone()[0])
    assert _encoded_secret() not in persisted
    assert _literal_secret() not in persisted
    assert "[REDACTED:" in persisted


def test_feedback_spool_and_replay_never_persist_raw_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(spool.ENV_SPOOL_DIR, str(tmp_path / "spool"))
    monkeypatch.delenv("QDRANT_URL", raising=False)
    db_path = tmp_path / "feedback-spool.db"
    service = MemoryService(db_path)
    service.init_db()
    claim = service.ingest("Safe feedback target claim.", [])
    query = f"How does auxiliary recall work {_encoded_secret()}"

    path = spool.append(
        db_path,
        "feedback",
        {"claim_ids": [claim.id], "query_text": query},
    )
    _assert_tree_absent(path.parent, _encoded_secret(), _literal_secret())
    result = spool_drain.run(service)

    assert result["drained"] == 1
    with sqlite3.connect(db_path) as conn:
        persisted = str(conn.execute("SELECT query_text FROM usage_feedback").fetchone()[0])
    assert _encoded_secret() not in persisted
    assert "[REDACTED:" in persisted


def test_ro_access_spool_hashes_sanitized_query_not_sensitive_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(spool.ENV_SPOOL_DIR, str(tmp_path / "spool"))
    db_path = tmp_path / "access-spool.db"
    service = MemoryService(db_path)
    service.init_db()
    query = f"How does auxiliary recall work {_encoded_secret()}"
    safe_query, _ = sanitize_persisted_text(query)

    service._spool_accesses([1], query)

    envelopes = [
        json.loads(line)
        for path in spool.spool_dir_for(db_path).glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    by_op = {envelope["op"]: envelope for envelope in envelopes}
    expected_hash = hashlib.sha256(safe_query.encode("utf-8")).hexdigest()[:12]
    assert by_op["access"]["payload"]["query_hash"] == expected_hash
    assert by_op["feedback"]["payload"]["query_text"] == safe_query


def test_spooled_ingest_preserves_holder_and_records_boundary_findings_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(spool.ENV_SPOOL_DIR, str(tmp_path / "spool"))
    db_path = tmp_path / "holder-spool.db"
    service = MemoryService(db_path)
    service.init_db()
    secret = _encoded_secret()

    spool.append(
        db_path,
        "ingest",
        {
            "text": f"Holder-attributed safe claim with {secret}",
            "citations": [],
            "holder": "codex",
            "scope": "project:aux-test",
            "source_agent": "aux-test",
        },
        idempotency_key="aux-holder-boundary",
    )
    first = spool_drain.run(service)

    claims = service.store.list_claims(holder="codex", include_archived=True)
    assert first["drained"] == 1
    assert len(claims) == 1
    with service.store.connect() as conn:
        events = conn.execute(
            """SELECT COUNT(*) FROM events
               WHERE claim_id = ? AND event_type = 'policy_decision'
                 AND details = 'sensitive_redaction_applied'""",
            (claims[0].id,),
        ).fetchone()[0]
    assert events == 1


def test_daily_and_ghost_notes_hide_legacy_sensitive_feedback(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-feedback.db"
    service = MemoryService(db_path)
    service.init_db()
    tracker = FeedbackTracker(str(db_path))
    tracker.ensure_tables()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw_query = f"legacy topic {_encoded_secret()}"
    with sqlite3.connect(db_path) as conn:
        for index in range(3):
            conn.execute(
                """INSERT INTO usage_feedback
                   (id, claim_id, query_text, timestamp, was_returned)
                   VALUES (?, ?, ?, ?, 1)""",
                (f"legacy-{index}", 1, raw_query, f"{today}T00:00:0{index}Z"),
            )
        conn.commit()

    note = generate_daily_note(str(db_path), today)
    ghosts = find_ghost_notes(str(db_path), min_references=1)
    rendered = json.dumps({"note": note, "ghosts": ghosts})
    assert _encoded_secret() not in rendered
    assert _literal_secret() not in rendered


def test_daily_note_sanitizes_legacy_claim_type_before_rendering(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-claim-type.db"
    service = MemoryService(db_path)
    service.init_db()
    claim = service.ingest("Safe claim body for a legacy metadata row.", [])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    secret = _encoded_secret()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE claims SET claim_type = ?, created_at = ? WHERE id = ?",
            (secret, f"{today}T00:00:00Z", claim.id),
        )
        conn.commit()

    note = generate_daily_note(str(db_path), today)

    assert secret not in note["note"]
    assert _literal_secret() not in note["note"]
    assert "[REDACTED:" in note["note"]


def test_daily_note_export_rejects_path_shaped_date_before_write(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "daily-path.db"
    service = MemoryService(db_path)
    service.init_db()
    output = tmp_path / "notes"

    with pytest.raises(ValueError, match="date"):
        export_daily_note_md(str(db_path), str(output), "../escaped")

    assert not (tmp_path / "escaped.md").exists()


@pytest.mark.parametrize("secret_turn", ["assistant", "user"])
def test_rule_miner_skips_sensitive_legacy_window_before_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secret_turn: str,
) -> None:
    db_path = tmp_path / f"rule-miner-{secret_turn}.db"
    service = MemoryService(db_path, workspace_root=tmp_path)
    service.init_db()
    verbatim_store.ensure_verbatim_schema(str(db_path))
    secret = _encoded_secret()
    assistant = "I hardcoded the path directly into the application configuration."
    user = "No, do not hardcode that path; use an environment variable instead."
    if secret_turn == "assistant":
        assistant = f"{assistant} {secret}"
    else:
        user = f"{user} {secret}"
    with sqlite3.connect(db_path) as conn:
        for role, content in (("assistant", assistant), ("user", user)):
            row_id = int(
                conn.execute(
                    """INSERT INTO verbatim_memories
                       (session_id, role, content, scope, timestamp, source_agent)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        "legacy-rule-session",
                        role,
                        content,
                        "project:aux-test",
                        "2026-07-11T00:00:00Z",
                        "legacy-test",
                    ),
                ).lastrowid
            )
            conn.execute(
                "INSERT INTO verbatim_fts(rowid, content) VALUES (?, ?)",
                (row_id, content),
            )
        conn.commit()

    calls: list[str] = []

    def _must_not_extract(window: str):
        calls.append(window)
        return None

    monkeypatch.setattr(rule_miner, "_extract_rule", _must_not_extract)
    stats = rule_miner.mine_rules(str(db_path), service, provider="claude_cli")

    assert calls == []
    assert stats["candidates"] == 1
    assert stats["skipped"] == 1
    assert stats["llm_calls"] == 0
    assert stats["last_id"] == 2


def test_stop_rule_miner_skips_sensitive_transcript_before_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "rule-stop.db"
    service = MemoryService(db_path, workspace_root=tmp_path)
    service.init_db()
    transcript = tmp_path / "session.jsonl"
    turns = (
        (
            "assistant",
            f"I hardcoded the path directly into the application. {_encoded_secret()}",
        ),
        (
            "user",
            "No, do not hardcode that path; use an environment variable instead.",
        ),
    )
    transcript.write_text(
        "\n".join(
            json.dumps(
                {
                    "type": role,
                    "message": {
                        "role": role,
                        "content": [{"type": "text", "text": content}],
                    },
                }
            )
            for role, content in turns
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    def _must_not_extract(window: str):
        calls.append(window)
        return None

    monkeypatch.setattr(rule_miner, "_extract_rule", _must_not_extract)
    stats = rule_miner.mine_transcript_rules(
        str(transcript),
        service,
        scope="project:aux-test",
        provider="claude_cli",
    )

    assert calls == []
    assert stats == {"windows": 1, "llm_calls": 0, "ingested": 0, "skipped": 1}


def test_rule_miner_rejects_encoded_sensitive_model_output() -> None:
    assert rule_miner._is_sensitive_rule(
        {
            "trigger": "auth configuration",
            "action": f"use this encoded value {_encoded_secret()}",
            "rationale": "continuous integration",
        }
    )
