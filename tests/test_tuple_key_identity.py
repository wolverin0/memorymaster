"""T-1877 (review F6): one canonical tuple identity, computed in Python, persisted and used by every lookup."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern import contradiction_probe

migration = importlib.import_module("memorymaster.stores.migrations.0031_claim_tuple_key")

CASES = [
    ("ÁPI", "ápi"),  # Unicode: SQLite lower() is ASCII only
    ("API  Gateway", "api gateway"),  # internal whitespace run: SQL trim() does not collapse it
]


def _confirmed(service: MemoryService, subject: str, key: str, obj: str = "v1"):
    claim = service.ingest(
        text=f"{subject} serves {obj}", citations=[CitationInput(source=f"tuple-key-{key}")],
        idempotency_key=key, subject=subject, predicate="serves", object_value=obj,
    )
    transition_claim(service.store, claim.id, "confirmed", reason="fixture", event_type="validator")
    return service.store.get_claim(claim.id, include_citations=False)


@pytest.fixture()
def service(tmp_path: Path) -> MemoryService:
    svc = MemoryService(str(tmp_path / "tuple-key.db"))
    svc.init_db()
    return svc


@pytest.mark.parametrize(("stored", "probed"), CASES)
def test_lookup_agrees_with_python_identity(service: MemoryService, stored: str, probed: str) -> None:
    confirmed = _confirmed(service, stored, "a")
    other = service.ingest(
        text=f"{probed} serves v2", citations=[CitationInput(source="tuple-key-b")],
        idempotency_key="b", subject=probed, predicate="serves", object_value="v2",
    )
    # The probe's skip rule says the deterministic path owns this pair ...
    assert contradiction_probe._deterministic_relation(confirmed, other)
    # ... so the deterministic lookup must really find it.
    found = service.store.find_confirmed_by_tuple(
        subject=probed, predicate="serves", scope=other.scope, exclude_claim_id=other.id,
        tenant_id=other.tenant_id, visibility=other.visibility, source_agent=other.source_agent,
    )
    assert [c.id for c in found] == [confirmed.id]


def test_migration_backfills_the_key_and_unkeyed_rows_still_match(service: MemoryService) -> None:
    confirmed = _confirmed(service, "API  Gateway", "a")
    ascii_row = _confirmed(service, "Router", "r")
    with service.store.connect() as conn:
        assert conn.execute("SELECT tuple_key FROM claims WHERE id = ?", (confirmed.id,)).fetchone()[0]
        conn.execute("UPDATE claims SET tuple_key = NULL")  # a database as it was before the migration
        conn.commit()
    # Unkeyed legacy rows keep the old ASCII case/trim match.
    legacy = service.store.find_confirmed_by_tuple(
        subject=" router ", predicate="SERVES", scope=ascii_row.scope, tenant_id=ascii_row.tenant_id,
        visibility=ascii_row.visibility, source_agent=ascii_row.source_agent,
    )
    assert [c.id for c in legacy] == [ascii_row.id]

    with service.store.connect() as conn:
        migration.apply_sqlite(conn)
        keys = {r[0]: r[1] for r in conn.execute("SELECT id, tuple_key FROM claims")}
    assert keys[confirmed.id] and keys[confirmed.id] != keys[ascii_row.id]


def test_legacy_steward_rewrite_keeps_the_key_in_step(service: MemoryService) -> None:
    from memorymaster.core.models import tuple_key
    from memorymaster.govern import llm_steward

    claim = service.ingest(
        text="gateway notes", citations=[CitationInput(source="tuple-key-steward")],
        idempotency_key="steward", subject="old subject", predicate="old predicate", object_value="v1",
    )
    with service.store.connect() as conn:
        version = conn.execute("SELECT version FROM claims WHERE id = ?", (claim.id,)).fetchone()[0]
        assert llm_steward._confirm_candidate_cas(
            conn, claim_id=claim.id, version=version, subject="API  Gateway", predicate="Serves",
            object_value="v1", confidence=0.9,
        )
        conn.commit()
        stored = conn.execute("SELECT tuple_key FROM claims WHERE id = ?", (claim.id,)).fetchone()[0]
    assert stored == tuple_key("api gateway", "serves")
