"""Tests for hierarchical human-readable claim IDs (P4 feature #21)."""
from __future__ import annotations

import tempfile
import os
from pathlib import Path

import pytest

from memorymaster.models import CitationInput
from memorymaster.service import MemoryService
from memorymaster.storage import SQLiteStore, generate_human_id_hash, generate_top_level_human_id


def _fresh_db() -> str:
    return os.path.join(tempfile.mkdtemp(), "test_hid.db")


class TestHumanIdGeneration:
    def test_generate_human_id_hash_deterministic(self):
        assert generate_human_id_hash("hello") == generate_human_id_hash("hello")
        assert len(generate_human_id_hash("hello")) == 4

    def test_generate_human_id_hash_differs_for_different_input(self):
        assert generate_human_id_hash("python") != generate_human_id_hash("node")

    def test_generate_top_level_human_id_format(self):
        hid = generate_top_level_human_id("auth", "Auth uses JWT")
        assert hid.startswith("mm-")
        assert len(hid) == 7  # mm- + 4 hex chars

    def test_generate_top_level_human_id_uses_subject_when_available(self):
        hid_with_subject = generate_top_level_human_id("python", "Python version is 3.12")
        hid_from_text = generate_top_level_human_id(None, "Python version is 3.12")
        assert hid_with_subject != hid_from_text  # subject seed differs from text seed


class TestHumanIdMigration:
    def test_init_db_adds_human_id_column(self):
        db = _fresh_db()
        store = SQLiteStore(db)
        store.init_db()
        with store.connect() as conn:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(claims)").fetchall()]
        assert "human_id" in cols

    def test_init_db_creates_unique_index(self):
        db = _fresh_db()
        store = SQLiteStore(db)
        store.init_db()
        with store.connect() as conn:
            indexes = [
                row[1]
                for row in conn.execute("PRAGMA index_list(claims)").fetchall()
            ]
        assert "idx_claims_human_id" in indexes

    def test_backfill_assigns_human_ids_to_existing_claims(self):
        db = _fresh_db()
        store = SQLiteStore(db)
        store.init_db()
        # Create claims (they get human_id on insert)
        cite = CitationInput(source="test")
        c1 = store.create_claim("claim one", [cite], subject="topic_a")
        c2 = store.create_claim("claim two", [cite], subject="topic_b")
        assert c1.human_id is not None
        assert c2.human_id is not None
        # Null them out
        with store.connect() as conn:
            conn.execute("UPDATE claims SET human_id = NULL")
            conn.commit()
        # Re-run init to trigger backfill
        store.init_db()
        r1 = store.get_claim(c1.id)
        r2 = store.get_claim(c2.id)
        assert r1.human_id is not None
        assert r2.human_id is not None


class TestHumanIdOnCreate:
    def test_new_claim_gets_human_id(self):
        db = _fresh_db()
        svc = MemoryService(db, workspace_root=Path.cwd())
        svc.init_db()
        claim = svc.ingest(
            text="Python is great",
            citations=[CitationInput(source="docs")],
            subject="python",
        )
        assert claim.human_id is not None
        assert claim.human_id.startswith("mm-")

    def test_collision_resolution(self):
        db = _fresh_db()
        svc = MemoryService(db, workspace_root=Path.cwd())
        svc.init_db()
        c1 = svc.ingest(
            text="Python is great",
            citations=[CitationInput(source="docs")],
            subject="python",
        )
        c2 = svc.ingest(
            text="Python is awesome",
            citations=[CitationInput(source="docs")],
            subject="python",
            predicate="quality",
        )
        assert c1.human_id != c2.human_id
        # Second claim should have a collision suffix
        assert "~" in c2.human_id

    def test_different_subjects_different_ids(self):
        db = _fresh_db()
        svc = MemoryService(db, workspace_root=Path.cwd())
        svc.init_db()
        c1 = svc.ingest(
            text="Python is great",
            citations=[CitationInput(source="docs")],
            subject="python",
        )
        c2 = svc.ingest(
            text="Node is fast",
            citations=[CitationInput(source="docs")],
            subject="node",
        )
        assert c1.human_id != c2.human_id


class TestHumanIdLookup:
    def test_get_claim_by_human_id(self):
        db = _fresh_db()
        svc = MemoryService(db, workspace_root=Path.cwd())
        svc.init_db()
        original = svc.ingest(
            text="Test claim",
            citations=[CitationInput(source="test")],
            subject="lookup",
        )
        found = svc.store.get_claim_by_human_id(original.human_id)
        assert found is not None
        assert found.id == original.id

    def test_get_claim_by_human_id_not_found(self):
        db = _fresh_db()
        store = SQLiteStore(db)
        store.init_db()
        assert store.get_claim_by_human_id("mm-0000") is None

    def test_resolve_claim_id_numeric_string(self):
        db = _fresh_db()
        svc = MemoryService(db, workspace_root=Path.cwd())
        svc.init_db()
        claim = svc.ingest(
            text="Test", citations=[CitationInput(source="t")], subject="x"
        )
        assert svc.store.resolve_claim_id(str(claim.id)) == claim.id

    def test_resolve_claim_id_human_id(self):
        db = _fresh_db()
        svc = MemoryService(db, workspace_root=Path.cwd())
        svc.init_db()
        claim = svc.ingest(
            text="Test", citations=[CitationInput(source="t")], subject="x"
        )
        assert svc.store.resolve_claim_id(claim.human_id) == claim.id

    def test_resolve_claim_id_int(self):
        db = _fresh_db()
        store = SQLiteStore(db)
        store.init_db()
        assert store.resolve_claim_id(42) == 42

    def test_resolve_claim_id_invalid_raises(self):
        db = _fresh_db()
        store = SQLiteStore(db)
        store.init_db()
        with pytest.raises(ValueError, match="No claim found"):
            store.resolve_claim_id("mm-nonexistent")


class TestHumanIdDerivedFrom:
    def test_backfill_creates_child_id(self):
        db = _fresh_db()
        svc = MemoryService(db, workspace_root=Path.cwd())
        svc.init_db()
        parent = svc.ingest(
            text="Parent claim",
            citations=[CitationInput(source="docs")],
            subject="auth",
        )
        child = svc.ingest(
            text="Child claim",
            citations=[CitationInput(source="docs")],
            subject="jwt",
        )
        svc.add_claim_link(child.id, parent.id, "derived_from")
        # Null out child human_id to simulate backfill
        with svc.store.connect() as conn:
            conn.execute("UPDATE claims SET human_id = NULL WHERE id = ?", (child.id,))
            conn.commit()
        # Re-init triggers backfill
        svc.store.init_db()
        refreshed = svc.store.get_claim(child.id)
        assert refreshed.human_id is not None
        assert refreshed.human_id.startswith(parent.human_id + ".")
        assert refreshed.human_id == f"{parent.human_id}.1"

    def test_multiple_children_get_sequential_ids(self):
        db = _fresh_db()
        svc = MemoryService(db, workspace_root=Path.cwd())
        svc.init_db()
        parent = svc.ingest(
            text="Parent", citations=[CitationInput(source="d")], subject="root"
        )
        child1 = svc.ingest(
            text="Child 1", citations=[CitationInput(source="d")], subject="c1"
        )
        child2 = svc.ingest(
            text="Child 2", citations=[CitationInput(source="d")], subject="c2"
        )
        svc.add_claim_link(child1.id, parent.id, "derived_from")
        svc.add_claim_link(child2.id, parent.id, "derived_from")
        # Null out children
        with svc.store.connect() as conn:
            conn.execute(
                "UPDATE claims SET human_id = NULL WHERE id IN (?, ?)",
                (child1.id, child2.id),
            )
            conn.commit()
        svc.store.init_db()
        r1 = svc.store.get_claim(child1.id)
        r2 = svc.store.get_claim(child2.id)
        assert r1.human_id == f"{parent.human_id}.1"
        assert r2.human_id == f"{parent.human_id}.2"


class TestHumanIdInClaimOutput:
    def test_claim_dataclass_includes_human_id(self):
        db = _fresh_db()
        svc = MemoryService(db, workspace_root=Path.cwd())
        svc.init_db()
        claim = svc.ingest(
            text="Test", citations=[CitationInput(source="t")], subject="x"
        )
        from dataclasses import asdict

        d = asdict(claim)
        assert "human_id" in d
        assert d["human_id"] is not None
        assert d["human_id"].startswith("mm-")


class TestCliResolveClaimId:
    def test_resolve_with_numeric_string(self):
        from memorymaster.cli import _resolve_claim_id

        db = _fresh_db()
        svc = MemoryService(db, workspace_root=Path.cwd())
        svc.init_db()
        claim = svc.ingest(
            text="Test", citations=[CitationInput(source="t")], subject="x"
        )
        assert _resolve_claim_id(svc, str(claim.id)) == claim.id

    def test_resolve_with_human_id(self):
        from memorymaster.cli import _resolve_claim_id

        db = _fresh_db()
        svc = MemoryService(db, workspace_root=Path.cwd())
        svc.init_db()
        claim = svc.ingest(
            text="Test", citations=[CitationInput(source="t")], subject="x"
        )
        assert _resolve_claim_id(svc, claim.human_id) == claim.id

    def test_resolve_with_int(self):
        from memorymaster.cli import _resolve_claim_id

        db = _fresh_db()
        svc = MemoryService(db, workspace_root=Path.cwd())
        svc.init_db()
        assert _resolve_claim_id(svc, 42) == 42
