from __future__ import annotations

import os

import pytest

from memorymaster.models import CitationInput
from memorymaster.service import MemoryService


@pytest.mark.postgres
def test_postgres_smoke_parity():
    dsn = os.getenv("MEMORYMASTER_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("MEMORYMASTER_TEST_POSTGRES_DSN is not set")

    service = MemoryService(dsn, workspace_root=".")
    service.init_db()

    # best-effort cleanup for deterministic run
    with service.store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM citations")
            cur.execute("SELECT to_regclass('public.claim_embeddings') AS tbl")
            emb_tbl = cur.fetchone()
            if emb_tbl and emb_tbl["tbl"] is not None:
                cur.execute("DELETE FROM claim_embeddings")
            cur.execute("DELETE FROM events")
            cur.execute("DELETE FROM claims")

    service.ingest(
        text="Server IP is 192.168.100.186",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="old")],
        subject="server",
        predicate="ip_address",
        object_value="192.168.100.186",
    )
    service.ingest(
        text="Server IP is 10.0.0.2",
        citations=[CitationInput(source="session://chat", locator="turn-2", excerpt="new")],
        subject="server",
        predicate="ip_address",
        object_value="10.0.0.2",
    )

    result = service.run_cycle(policy_mode="legacy", min_citations=1, min_score=0.5)
    assert result["validator"]["processed"] >= 2

    rows = service.query("server ip", retrieval_mode="hybrid", limit=10, allow_sensitive=True)
    assert len(rows) >= 1
