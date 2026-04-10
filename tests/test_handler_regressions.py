"""Regression tests for handlers that had F821/TypeError bugs after the
cli.py + storage.py refactor.

Every handler listed here had a silent undefined-name or missing-kwarg bug
that slipped past CI because no test exercised the specific code path.
These tests are intentionally minimal smoke tests — they only need to
invoke each handler with arguments that would trigger the original bug.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

import pytest

from memorymaster.service import MemoryService


@pytest.fixture
def service(tmp_path):
    db = tmp_path / "mm-regress.db"
    svc = MemoryService(str(db), workspace_root=tmp_path)
    svc.init_db()
    return svc


@pytest.fixture
def ingested_claim(service):
    """Create a claim so handlers that operate on a claim_id have something to work with."""
    from memorymaster.models import CitationInput
    claim = service.ingest(
        text="Regression-fixture claim",
        citations=[CitationInput(source="pytest")],
        claim_type="fact",
        subject="regression",
        predicate="exists",
    )
    return claim


# ---------------------------------------------------------------------------
# _handle_history — was missing _score_str_from_payload import
# ---------------------------------------------------------------------------
class TestHandleHistory:
    def test_text_mode_with_events(self, service, ingested_claim, capsys):
        """Triggers the _score_str_from_payload code path at cli_handlers_basic.py:513.

        Before the fix this raised NameError: name '_score_str_from_payload' is not defined.
        """
        from memorymaster.cli_handlers_basic import _handle_history

        args = argparse.Namespace(
            claim_id=str(ingested_claim.id),
            json_output=False,
            limit=50,
        )
        rc = _handle_history(args, service, None, service.store.db_path)
        assert rc == 0
        captured = capsys.readouterr()
        assert "ingest" in captured.out or "created" in captured.out.lower() or str(ingested_claim.id) in captured.out

    def test_json_mode(self, service, ingested_claim, capsys):
        from memorymaster.cli_handlers_basic import _handle_history

        args = argparse.Namespace(
            claim_id=str(ingested_claim.id),
            json_output=True,
            limit=50,
        )
        rc = _handle_history(args, service, None, service.store.db_path)
        assert rc == 0


# ---------------------------------------------------------------------------
# _handle_extract_claims --ingest — was missing CitationInput import
# ---------------------------------------------------------------------------
class TestHandleExtractClaims:
    def test_handler_importable(self):
        """Regression for F821: CitationInput was undefined in cli_handlers_curation.

        We only need to verify the import and the handler reference the symbol.
        Full extraction requires an LLM provider we don't want to mock here.
        """
        from memorymaster import cli_handlers_curation
        assert hasattr(cli_handlers_curation, "_handle_extract_claims")
        # The fix added `from memorymaster.models import CitationInput` at the top of
        # cli_handlers_curation — this getattr access fails if the module raised NameError
        # during import.
        assert cli_handlers_curation.CitationInput is not None


# ---------------------------------------------------------------------------
# _handle_federated_query — was missing _SCORE_KEYS + print_claim imports
# ---------------------------------------------------------------------------
class TestHandleFederatedQuery:
    def test_handler_importable_with_required_symbols(self):
        """Regression for F821: _SCORE_KEYS and print_claim used but not imported."""
        from memorymaster import cli_handlers_curation
        assert cli_handlers_curation._SCORE_KEYS is not None
        assert cli_handlers_curation.print_claim is not None
        assert hasattr(cli_handlers_curation, "_handle_federated_query")

    def test_json_mode_empty_databases(self, tmp_path, capsys):
        """Actually call the handler in JSON mode against two empty databases.

        Before the fix this raised NameError: name '_SCORE_KEYS' is not defined
        as soon as the code path that aggregates scores was reached.
        """
        from memorymaster.cli_handlers_curation import _handle_federated_query

        db1 = tmp_path / "mm1.db"
        db2 = tmp_path / "mm2.db"
        MemoryService(str(db1), workspace_root=tmp_path).init_db()
        MemoryService(str(db2), workspace_root=tmp_path).init_db()

        args = argparse.Namespace(
            text="anything",
            dbs=f"{db1},{db2}",
            limit=5,
            mode="hybrid",
            json_output=True,
        )
        # Create a temporary service for the primary — federated_query ignores it for
        # aggregation but the dispatch signature requires one.
        primary = MemoryService(str(db1), workspace_root=tmp_path)
        rc = _handle_federated_query(args, primary, None, str(db1))
        assert rc == 0


# ---------------------------------------------------------------------------
# _handle_ghost_notes --json — was missing query_ms kwarg to _json_envelope
# ---------------------------------------------------------------------------
class TestHandleGhostNotes:
    def test_json_mode_does_not_raise_typeerror(self, service, tmp_path, capsys):
        """Regression for TypeError: _json_envelope() missing required keyword-only
        argument 'query_ms' at cli_handlers_curation.py:576.
        """
        from memorymaster.cli_handlers_curation import _handle_ghost_notes

        args = argparse.Namespace(json_output=True)
        rc = _handle_ghost_notes(args, service, None, service.store.db_path)
        assert rc == 0
        captured = capsys.readouterr()
        # Should be valid JSON with the standard envelope
        import json as _json
        parsed = _json.loads(captured.out)
        assert parsed["ok"] is True
        assert "data" in parsed
        assert "meta" in parsed
        assert "query_ms" in parsed["meta"]

    def test_text_mode(self, service, capsys):
        from memorymaster.cli_handlers_curation import _handle_ghost_notes

        args = argparse.Namespace(json_output=False)
        rc = _handle_ghost_notes(args, service, None, service.store.db_path)
        assert rc == 0
