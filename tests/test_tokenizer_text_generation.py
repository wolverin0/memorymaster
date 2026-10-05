"""The tokenizer's corpus statistics survive writes that cannot change them (O-0098).

Measured 2026-10-04: on a 45k-claim corpus warm recall took 69 ms, but the first call
after one write took 681 ms, because every write bumped `corpus_generation` (16 columns,
including confidence and updated_at) and the tokenizer rescanned the whole corpus. Its
statistics depend only on claim text and on which claims are live (status), plus
entity aliases; they now key on a counter that only those changes advance.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core import lifecycle
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall import recall_tokenizer
from memorymaster.recall.qdrant_outbox import ENV_OUTBOX_DIR


@pytest.fixture()
def svc(tmp_path: Path, monkeypatch) -> MemoryService:
    monkeypatch.setenv(ENV_OUTBOX_DIR, str(tmp_path / "outbox"))
    monkeypatch.delenv("QDRANT_URL", raising=False)
    service = MemoryService(tmp_path / "t.db", workspace_root=tmp_path)
    service.init_db()
    return service


def _claim(svc: MemoryService, text: str) -> int:
    return svc.store.create_claim(text=text, citations=[CitationInput(source="test://gen")], scope="project").id


def _key(svc: MemoryService) -> int:
    return recall_tokenizer.read_text_generation(str(svc.store.db_path))


def test_confidence_and_validation_writes_keep_the_statistics_key(svc) -> None:
    claim_id = _claim(svc, "cloudflare tunnel failover for the hermes sync")
    before = _key(svc)
    with svc.store.connect() as conn:  # the writes the steward and validators do all day
        conn.execute("UPDATE claims SET confidence = 0.91, updated_at = 'x', last_validated_at = 'y', "
                     "tier = 'core' WHERE id = ?", (claim_id,))
    assert _key(svc) == before


def test_text_status_insert_and_alias_changes_advance_it(svc) -> None:
    claim_id = _claim(svc, "sqlite backup restore drill")
    steps = [_key(svc)]
    with svc.store.connect() as conn:
        conn.execute("UPDATE claims SET text = 'sqlite backup restore drill weekly' WHERE id = ?", (claim_id,))
    steps.append(_key(svc))
    lifecycle.transition_claim(svc.store, claim_id, "archived", reason="test", event_type="compactor")
    steps.append(_key(svc))
    _claim(svc, "another live claim")
    steps.append(_key(svc))
    with svc.store.connect() as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'entity_aliases'").fetchone():
            entity = conn.execute("SELECT id FROM entities LIMIT 1").fetchone()
            if entity is None:
                conn.execute("INSERT INTO entities(canonical_name, entity_type, created_at, updated_at) "
                             "VALUES ('hermes', 'system', 'now', 'now')")
                entity = conn.execute("SELECT id FROM entities LIMIT 1").fetchone()
            conn.execute("INSERT INTO entity_aliases(entity_id, alias, original_form, created_at) "
                         "VALUES (?, 'hermes-vm', 'Hermes-VM', 'now')",
                         (entity[0],))
            steps.append(None)
    if steps[-1] is None:
        steps[-1] = _key(svc)
    assert all(later > earlier for earlier, later in zip(steps, steps[1:])), steps


def test_a_confidence_write_does_not_force_a_rescan(svc) -> None:
    claim_id = _claim(svc, "mikrotik router dns failover cloudflare")
    recall_tokenizer._corpus_stats.cache_clear()
    db = str(svc.store.db_path)
    recall_tokenizer.extract_query_tokens("mikrotik dns failover", db)
    with svc.store.connect() as conn:
        conn.execute("UPDATE claims SET confidence = 0.4, updated_at = 'z' WHERE id = ?", (claim_id,))
    recall_tokenizer.extract_query_tokens("mikrotik dns failover", db)
    assert recall_tokenizer._corpus_stats.cache_info().misses == 1
