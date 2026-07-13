"""Adversarial scope-namespace coverage for direct identity consumers."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from memorymaster.bridges.db_merge import merge_databases
from memorymaster.bridges.dream_bridge import dream_ingest
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.knowledge.transcript_miner import mine_transcript
from memorymaster.recall.claim_edges import MENTION_KIND, extract_edges_for_claim
from memorymaster.stores.storage import SQLiteStore
from memorymaster.surfaces.cli_helpers import _resolve_claim_id


TENANT = "tenant-scope-bypass"
SCOPE_A = "project:scope-bypass-a"
SCOPE_B = "project:scope-bypass-b"
CITATIONS = [CitationInput(source="scope-bypass-red", locator="fixture")]


def _store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.init_db()
    return store


def _public_claim(
    store: SQLiteStore,
    *,
    scope: str,
    key: str,
    text: str,
    tenant_id: str | None = TENANT,
):
    return store.create_claim(
        text,
        CITATIONS,
        idempotency_key=key,
        subject="scope-bypass",
        predicate="uses",
        scope=scope,
        tenant_id=tenant_id,
        source_agent="fixture-writer",
        visibility="public",
    )


def test_db_merge_preserves_same_public_key_in_disjoint_scopes(tmp_path: Path) -> None:
    target_path = tmp_path / "target.db"
    source_path = tmp_path / "source.db"
    target = _store(target_path)
    source = _store(source_path)
    _public_claim(
        target,
        scope=SCOPE_A,
        key="same-cross-scope-key",
        text="Target scope payload.",
    )
    _public_claim(
        source,
        scope=SCOPE_B,
        key="same-cross-scope-key",
        text="Source scope payload.",
    )

    stats = merge_databases(str(target_path), str(source_path))

    with target.connect() as conn:
        scopes = {
            str(row[0])
            for row in conn.execute(
                "SELECT scope FROM claims WHERE idempotency_key = ?",
                ("same-cross-scope-key",),
            )
        }
    assert stats["merged"] == 1
    assert scopes == {SCOPE_A, SCOPE_B}


def test_transcript_dedup_is_exact_scope_local(tmp_path: Path) -> None:
    db_path = tmp_path / "transcript.db"
    store = _store(db_path)
    text = "The root cause was a stale cache entry in the scoped worker."
    digest = hashlib.sha256(text[:500].strip().lower().encode()).hexdigest()[:16]
    key = f"transcript-{digest}"
    _public_claim(
        store,
        scope=SCOPE_A,
        key=key,
        text="Existing other-scope row.",
        tenant_id=None,
    )
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"role": "assistant", "content": text}) + "\n",
        encoding="utf-8",
    )

    stats = mine_transcript(
        transcript,
        str(db_path),
        scope=SCOPE_B,
        min_length=10,
    )

    with store.connect() as conn:
        scopes = {
            str(row[0])
            for row in conn.execute(
                "SELECT scope FROM claims WHERE idempotency_key = ?",
                (key,),
            )
        }
    assert stats["ingested"] == 1
    assert scopes == {SCOPE_A, SCOPE_B}


def test_dream_dedup_is_exact_project_scope_local(tmp_path: Path) -> None:
    db_path = tmp_path / "dream.db"
    store = _store(db_path)
    marker = "auto-dream:scoped-note.md"
    _public_claim(
        store,
        scope=SCOPE_A,
        key=marker,
        text="Existing other-scope row.",
        tenant_id=None,
    )
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "scoped-note.md").write_text(
        "\n".join(
            (
                "---",
                'name: "scoped-note"',
                'description: "Scoped direct-ingest regression fixture"',
                'type: "project"',
                "---",
                "",
                "The scoped retrieval bridge uses a bounded cache.",
            )
        ),
        encoding="utf-8",
    )

    with patch.dict("os.environ", {"CLAUDE_MEMORY_DIR": str(memory_dir)}):
        stats = dream_ingest(str(db_path), use_spool=False)

    with store.connect() as conn:
        scopes = {
            str(row[0])
            for row in conn.execute(
                "SELECT scope FROM claims WHERE idempotency_key = ?",
                (marker,),
            )
        }
    assert stats["ingested"] == 1
    assert scopes == {SCOPE_A, "project"}


def test_human_reference_resolution_stays_in_source_scope() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(
            """
            CREATE TABLE claims (
                id INTEGER PRIMARY KEY,
                text TEXT NOT NULL,
                human_id TEXT,
                tenant_id TEXT,
                scope TEXT NOT NULL,
                visibility TEXT NOT NULL,
                source_agent TEXT
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO claims
                (id, text, human_id, tenant_id, scope, visibility, source_agent)
            VALUES (?, ?, ?, ?, ?, 'public', ?)
            """,
            (
                (10, "Wrong scope target", "mm-abcd", TENANT, SCOPE_A, "alice"),
                (20, "Exact scope target", "mm-abcd", TENANT, SCOPE_B, "bob"),
                (30, "See mm-abcd", "mm-3333", TENANT, SCOPE_B, "bob"),
            ),
        )

        edges = extract_edges_for_claim(conn, 30, "See mm-abcd")

        assert edges == [(30, 20, MENTION_KIND)]
    finally:
        conn.close()


def test_cli_passes_single_authorized_scope_to_human_id_resolution() -> None:
    calls: list[dict[str, object]] = []

    class Store:
        def resolve_claim_id(self, _identifier: str, **kwargs) -> int:
            calls.append(kwargs)
            return 20

    service = SimpleNamespace(
        tenant_id=TENANT,
        allowed_scopes={SCOPE_B},
        store=Store(),
    )

    assert _resolve_claim_id(service, "mm-abcd") == 20
    assert calls == [
        {"tenant_id": TENANT, "visibility": "public", "scope": SCOPE_B}
    ]


def test_direct_human_lookup_requires_scope_when_identity_is_ambiguous(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "ambiguous-human.db")
    first = _public_claim(
        store,
        scope=SCOPE_A,
        key="ambiguous-human-a",
        text="Same human identity seed.",
    )
    second = _public_claim(
        store,
        scope=SCOPE_B,
        key="ambiguous-human-b",
        text="Same human identity seed.",
    )
    assert first.human_id == second.human_id

    with pytest.raises(ValueError, match="(?i)(ambiguous|scope)"):
        store.get_claim_by_human_id(first.human_id, tenant_id=TENANT)


def test_cli_multi_scope_resolution_requests_ambiguity_checked_lookup() -> None:
    calls: list[dict[str, object]] = []

    class Store:
        def resolve_claim_id(self, _identifier: str, **kwargs) -> int:
            calls.append(kwargs)
            return 20

    service = SimpleNamespace(
        tenant_id=TENANT,
        allowed_scopes={SCOPE_A, SCOPE_B},
        store=Store(),
    )

    assert _resolve_claim_id(service, "mm-abcd") == 20
    assert calls == [
        {"tenant_id": TENANT, "visibility": "public", "scope": None}
    ]


@pytest.mark.parametrize(
    ("scope_allowlist", "expected_scope"),
    [([SCOPE_B], SCOPE_B), ([SCOPE_A, SCOPE_B], None)],
)
def test_claim_path_human_resolution_preserves_scope_ambiguity_contract(
    scope_allowlist: list[str],
    expected_scope: str | None,
) -> None:
    calls: list[dict[str, object]] = []

    class Store:
        def resolve_claim_id(self, _identifier: str, **kwargs) -> int:
            calls.append(kwargs)
            raise ValueError("fixture stops after identity resolution")

    service = MemoryService.__new__(MemoryService)
    service.tenant_id = TENANT
    service.require_tenant = True
    service.principal = "alice"
    service.allowed_scopes = frozenset({SCOPE_A, SCOPE_B})
    service.store = Store()

    assert service.query_claim_paths(
        "mm-abcd",
        scope_allowlist=scope_allowlist,
        requesting_agent="alice",
    ) == []
    assert calls == [{"tenant_id": TENANT, "scope": expected_scope}]


def test_claim_edge_null_scope_does_not_broaden_to_all_scopes() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(
            """
            CREATE TABLE claims (
                id INTEGER PRIMARY KEY,
                text TEXT NOT NULL,
                human_id TEXT,
                tenant_id TEXT,
                scope TEXT,
                visibility TEXT NOT NULL,
                source_agent TEXT
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO claims
                (id, text, human_id, tenant_id, scope, visibility, source_agent)
            VALUES (?, ?, ?, ?, ?, 'public', 'writer')
            """,
            (
                (10, "Wrong non-null scope", "mm-abcd", TENANT, SCOPE_A),
                (20, "Exact null scope", "mm-abcd", TENANT, None),
                (30, "See mm-abcd", "mm-3333", TENANT, None),
            ),
        )

        edges = extract_edges_for_claim(conn, 30, "See mm-abcd")

        assert edges == [(30, 20, MENTION_KIND)]
    finally:
        conn.close()
