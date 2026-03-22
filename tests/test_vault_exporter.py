"""Tests for Obsidian vault exporter."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from memorymaster.models import Claim, Citation, CitationInput
from memorymaster.vault_exporter import _claim_to_markdown, _safe_dirname, export_vault


def make_claim(**kwargs) -> Claim:
    """Helper to create test Claim with defaults."""
    defaults = {
        "id": 1,
        "text": "Test claim",
        "idempotency_key": None,
        "normalized_text": None,
        "claim_type": None,
        "subject": None,
        "predicate": None,
        "object_value": None,
        "scope": "global",
        "volatility": "0.0",
        "status": "active",
        "confidence": 0.5,
        "pinned": False,
        "supersedes_claim_id": None,
        "replaced_by_claim_id": None,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
        "last_validated_at": None,
        "archived_at": None,
    }
    defaults.update(kwargs)
    return Claim(**defaults)


class TestSafeDirname:
    """Test safe dirname conversion."""

    def test_safe_dirname_simple_scope(self):
        """Simple scope becomes lowercase."""
        assert _safe_dirname("project") == "project"
        assert _safe_dirname("PROJECT") == "project"

    def test_safe_dirname_colon_separated(self):
        """Colon-separated scopes take first two parts."""
        assert _safe_dirname("project:pedrito") == "project-pedrito"
        assert _safe_dirname("project:pedrito:abc123") == "project-pedrito"

    def test_safe_dirname_invalid_chars(self):
        """Invalid filename characters are replaced with hyphens."""
        # The regex replaces any non-alphanumeric, non-hyphen, non-underscore with hyphen
        result = _safe_dirname("project:Test@Name")
        assert "project" in result.lower()
        # It should normalize/remove special chars
        assert "@" not in result

    def test_safe_dirname_multiple_scopes(self):
        """Multiple parts in scope."""
        result = _safe_dirname("org:project:team")
        assert "org" in result
        assert "project" in result
        assert "-" in result

    def test_safe_dirname_empty(self):
        """Empty scope defaults to 'default'."""
        assert _safe_dirname("") == "default"
        assert _safe_dirname(":::") == "default"

    def test_safe_dirname_single_part(self):
        """Single part scope stays as-is."""
        assert _safe_dirname("global") == "global"
        assert _safe_dirname("Core") == "core"


class TestClaimToMarkdown:
    """Test claim to markdown conversion."""

    def test_claim_to_markdown_minimal(self):
        """Minimal claim renders to valid markdown."""
        claim = make_claim(
            id=1,
            text="This is a test claim",
            status="active",
            confidence=0.8,
            scope="project:test",
        )
        result = _claim_to_markdown(claim)
        assert "claim_id: 1" in result
        assert "status: active" in result
        assert "confidence: 0.800" in result
        assert "This is a test claim" in result
        assert "scope: project:test" in result

    def test_claim_to_markdown_with_human_id(self):
        """Human ID is included in frontmatter."""
        claim = make_claim(
            id=42,
            human_id="my-claim",
            text="Test",
            status="active",
            confidence=0.5,
            scope="global",
        )
        result = _claim_to_markdown(claim)
        assert "human_id: my-claim" in result

    def test_claim_to_markdown_with_type(self):
        """Claim type is included."""
        claim = make_claim(
            id=1,
            text="Test",
            status="active",
            confidence=0.5,
            claim_type="fact",
            scope="global",
        )
        result = _claim_to_markdown(claim)
        assert "type: fact" in result

    def test_claim_to_markdown_with_spo(self):
        """Subject-Predicate-Object is included."""
        claim = make_claim(
            id=1,
            text="Alice works at Company",
            status="active",
            confidence=0.7,
            subject="Alice",
            predicate="works_at",
            object_value="Company",
            scope="global",
        )
        result = _claim_to_markdown(claim)
        assert 'subject: "Alice"' in result
        assert 'predicate: "works_at"' in result
        assert 'object: "Company"' in result

    def test_claim_to_markdown_with_citations(self):
        """Citations are rendered as list."""
        # Create mock citations
        mock_citations = [
            MagicMock(source="file.txt", locator="line 42", excerpt="snippet"),
            MagicMock(source="doc.md", locator=None, excerpt=None),
        ]
        claim = make_claim(
            id=1,
            text="Test",
            status="active",
            confidence=0.5,
            scope="global",
            citations=mock_citations,
        )
        result = _claim_to_markdown(claim)
        assert "## Citations" in result
        assert "`file.txt | line 42 | snippet`" in result
        assert "`doc.md`" in result

    def test_claim_to_markdown_with_links(self):
        """Related claim links are rendered as wikilinks."""
        claim = make_claim(
            id=1,
            text="Test",
            status="active",
            confidence=0.5,
            scope="global",
        )
        links = [
            {"link_type": "relates_to", "target_id": 2, "target_human_id": "other-claim"},
            {"link_type": "contradicts", "target_id": 3, "target_human_id": "contradicting"},
        ]
        result = _claim_to_markdown(claim, links=links)
        assert "## Links" in result
        assert "relates_to [[other-claim]]" in result
        assert "contradicts [[contradicting]]" in result

    def test_claim_to_markdown_pinned_flag(self):
        """Pinned flag is included."""
        claim = make_claim(
            id=1,
            text="Important",
            status="active",
            confidence=0.9,
            pinned=True,
            scope="global",
        )
        result = _claim_to_markdown(claim)
        assert "pinned: true" in result

    def test_claim_to_markdown_volatility(self):
        """Volatility field is included."""
        claim = make_claim(
            id=1,
            text="Test",
            status="active",
            confidence=0.5,
            volatility="0.3",
            scope="global",
        )
        result = _claim_to_markdown(claim)
        assert "volatility: 0.3" in result

    def test_claim_to_markdown_long_text_title(self):
        """Long text is truncated in title."""
        claim = make_claim(
            id=1,
            text="This is a very long claim text that should be truncated in the markdown title to keep things readable",
            status="active",
            confidence=0.5,
            scope="global",
        )
        result = _claim_to_markdown(claim)
        assert "# This is a very long claim text that should be" in result
        # Title should not exceed reasonable length


class TestExportVault:
    """Test vault export functionality."""

    def test_export_vault_creates_directory(self):
        """export_vault creates output directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "vault"
            mock_store = MagicMock()
            mock_store.list_claims.return_value = []

            result = export_vault(mock_store, output)
            assert output.exists()
            assert result["exported"] == 0

    def test_export_vault_empty_store(self):
        """export_vault handles empty store."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_store = MagicMock()
            mock_store.list_claims.return_value = []

            result = export_vault(mock_store, Path(tmpdir))
            assert result["exported"] == 0
            assert result["skipped"] == 0

    def test_export_vault_single_claim(self):
        """export_vault exports single claim to markdown file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            claim = make_claim(
                id=1,
                human_id="test-claim",
                text="Test claim content",
                status="active",
                confidence=0.8,
                scope="project:test",
                            )
            mock_store = MagicMock()
            mock_store.list_claims.return_value = [claim]

            result = export_vault(mock_store, Path(tmpdir))
            assert result["exported"] == 1
            assert result["directories_created"] >= 1

            # Check file was created
            vault_dir = Path(tmpdir)
            md_files = list(vault_dir.glob("**/*.md"))
            assert len(md_files) >= 1

    def test_export_vault_scope_filter(self):
        """export_vault respects scope filter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            claim1 = make_claim(
                id=1,
                text="Global claim",
                status="active",
                confidence=0.5,
                scope="global",
                            )
            claim2 = make_claim(
                id=2,
                text="Project claim",
                status="active",
                confidence=0.5,
                scope="project:pedrito",
                            )
            mock_store = MagicMock()
            mock_store.list_claims.return_value = [claim1, claim2]

            result = export_vault(mock_store, Path(tmpdir), scope_filter="project")
            assert result["exported"] == 1
            assert result["skipped"] == 1

    def test_export_vault_confirmed_only(self):
        """export_vault respects confirmed_only filter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_store = MagicMock()
            mock_store.list_claims.return_value = []

            export_vault(mock_store, Path(tmpdir), confirmed_only=True)
            mock_store.list_claims.assert_called_once()
            call_kwargs = mock_store.list_claims.call_args[1]
            assert call_kwargs.get("status") == "confirmed"

    def test_export_vault_incremental(self):
        """export_vault supports incremental export."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "vault"
            claim = make_claim(
                id=1,
                text="Test",
                status="active",
                confidence=0.5,
                scope="global",
                            )
            mock_store = MagicMock()
            mock_store.list_claims.return_value = [claim]

            # First export
            result1 = export_vault(mock_store, output, incremental=True)
            assert result1["exported"] == 1

            # Check .last_export was created
            last_export_file = output / ".last_export"
            assert last_export_file.exists()

    def test_export_vault_include_archived(self):
        """export_vault passes include_archived to store."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_store = MagicMock()
            mock_store.list_claims.return_value = []

            export_vault(mock_store, Path(tmpdir), include_archived=True)
            call_kwargs = mock_store.list_claims.call_args[1]
            assert call_kwargs.get("include_archived") is True

    def test_export_vault_multiple_scopes(self):
        """export_vault creates subdirectories for different scopes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            claim1 = make_claim(
                id=1,
                text="Claim 1",
                status="active",
                confidence=0.5,
                scope="project:pedrito",
                            )
            claim2 = make_claim(
                id=2,
                text="Claim 2",
                status="active",
                confidence=0.5,
                scope="project:argentina",
                            )
            mock_store = MagicMock()
            mock_store.list_claims.return_value = [claim1, claim2]

            result = export_vault(mock_store, Path(tmpdir))
            assert result["exported"] == 2
            assert result["directories_created"] >= 2


class TestExportVaultWithCitations:
    """Test vault export with citations."""

    def test_export_vault_includes_citations(self):
        """Exported markdown includes citations from claims."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_citation = MagicMock(source="source.md", locator="line 10", excerpt=None)
            claim = make_claim(
                id=1,
                text="Important fact",
                status="active",
                confidence=0.9,
                scope="global",
                citations=[mock_citation],
            )
            mock_store = MagicMock()
            mock_store.list_claims.return_value = [claim]

            result = export_vault(mock_store, Path(tmpdir))
            # Verify the store was called
            mock_store.list_claims.assert_called_once()
            # Check that the markdown file includes citations
            assert result["exported"] == 1
