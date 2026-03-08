from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from memorymaster.models import CLAIM_LINK_TYPES, CitationInput, ClaimLink
from memorymaster.service import MemoryService


def _case_db(prefix: str) -> Path:
    fd, raw = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".db", dir=".tmp_cases")
    os.close(fd)
    Path(raw).unlink(missing_ok=True)
    return Path(raw)


def _make_service() -> MemoryService:
    db = _case_db("claim-links")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()
    return service


def _ingest_claim(service: MemoryService, text: str) -> int:
    claim = service.ingest(
        text=text,
        citations=[CitationInput(source="test", locator="loc")],
    )
    return claim.id


class TestClaimLinksStorage:
    def test_add_claim_link(self):
        svc = _make_service()
        a = _ingest_claim(svc, "Claim A")
        b = _ingest_claim(svc, "Claim B")

        link = svc.add_claim_link(a, b, "relates_to")
        assert isinstance(link, ClaimLink)
        assert link.source_id == a
        assert link.target_id == b
        assert link.link_type == "relates_to"
        assert link.id > 0

    def test_add_all_link_types(self):
        svc = _make_service()
        a = _ingest_claim(svc, "Claim A")
        b = _ingest_claim(svc, "Claim B")

        for lt in CLAIM_LINK_TYPES:
            link = svc.add_claim_link(a, b, lt)
            assert link.link_type == lt

    def test_duplicate_link_raises(self):
        svc = _make_service()
        a = _ingest_claim(svc, "Claim A")
        b = _ingest_claim(svc, "Claim B")

        svc.add_claim_link(a, b, "relates_to")
        with pytest.raises(ValueError, match="Link already exists"):
            svc.add_claim_link(a, b, "relates_to")

    def test_self_link_raises(self):
        svc = _make_service()
        a = _ingest_claim(svc, "Claim A")

        with pytest.raises(ValueError, match="must be different"):
            svc.add_claim_link(a, a, "relates_to")

    def test_invalid_link_type_raises(self):
        svc = _make_service()
        a = _ingest_claim(svc, "Claim A")
        b = _ingest_claim(svc, "Claim B")

        with pytest.raises(ValueError, match="Invalid link_type"):
            svc.add_claim_link(a, b, "invalid_type")

    def test_nonexistent_claim_raises(self):
        svc = _make_service()
        a = _ingest_claim(svc, "Claim A")

        with pytest.raises(ValueError, match="does not exist"):
            svc.add_claim_link(a, 99999, "relates_to")

    def test_remove_claim_link_specific_type(self):
        svc = _make_service()
        a = _ingest_claim(svc, "Claim A")
        b = _ingest_claim(svc, "Claim B")

        svc.add_claim_link(a, b, "relates_to")
        svc.add_claim_link(a, b, "supports")

        removed = svc.remove_claim_link(a, b, "relates_to")
        assert removed == 1

        links = svc.get_claim_links(a)
        assert len(links) == 1
        assert links[0].link_type == "supports"

    def test_remove_claim_link_all_types(self):
        svc = _make_service()
        a = _ingest_claim(svc, "Claim A")
        b = _ingest_claim(svc, "Claim B")

        svc.add_claim_link(a, b, "relates_to")
        svc.add_claim_link(a, b, "supports")

        removed = svc.remove_claim_link(a, b)
        assert removed == 2

        links = svc.get_claim_links(a)
        assert len(links) == 0

    def test_remove_nonexistent_returns_zero(self):
        svc = _make_service()
        removed = svc.remove_claim_link(1, 2, "relates_to")
        assert removed == 0

    def test_get_claim_links_both_directions(self):
        svc = _make_service()
        a = _ingest_claim(svc, "Claim A")
        b = _ingest_claim(svc, "Claim B")
        c = _ingest_claim(svc, "Claim C")

        svc.add_claim_link(a, b, "relates_to")
        svc.add_claim_link(c, a, "supports")

        links = svc.get_claim_links(a)
        assert len(links) == 2

    def test_get_linked_claims_filter_by_type(self):
        svc = _make_service()
        a = _ingest_claim(svc, "Claim A")
        b = _ingest_claim(svc, "Claim B")
        c = _ingest_claim(svc, "Claim C")

        svc.add_claim_link(a, b, "relates_to")
        svc.add_claim_link(a, c, "supports")

        relates = svc.get_linked_claims(a, link_type="relates_to")
        assert len(relates) == 1
        assert relates[0].target_id == b

        supports = svc.get_linked_claims(a, link_type="supports")
        assert len(supports) == 1
        assert supports[0].target_id == c

    def test_get_linked_claims_no_filter(self):
        svc = _make_service()
        a = _ingest_claim(svc, "Claim A")
        b = _ingest_claim(svc, "Claim B")

        svc.add_claim_link(a, b, "relates_to")
        svc.add_claim_link(a, b, "contradicts")

        all_links = svc.get_linked_claims(a)
        assert len(all_links) == 2

    def test_cascade_delete_on_claim_removal(self):
        """Links should be removed when a claim is deleted (CASCADE)."""
        svc = _make_service()
        a = _ingest_claim(svc, "Claim A")
        b = _ingest_claim(svc, "Claim B")

        svc.add_claim_link(a, b, "relates_to")

        # Directly delete claim to trigger CASCADE
        with svc.store.connect() as conn:
            conn.execute("DROP TRIGGER IF EXISTS trg_events_append_only_delete")
            conn.execute("DELETE FROM claims WHERE id = ?", (a,))
            conn.commit()

        links = svc.get_claim_links(b)
        assert len(links) == 0


class TestClaimLinksCLI:
    """CLI integration tests using tmp_path to avoid OneDrive sync issues."""

    @pytest.fixture()
    def cli_db(self, tmp_path: Path) -> Path:
        """Set up a fresh db using CLI init-db + ingest."""
        from memorymaster.cli import main

        db = tmp_path / "links_test.db"
        assert main(["--db", str(db), "init-db"]) == 0
        assert main([
            "--db", str(db), "ingest",
            "--text", "Claim A", "--source", "test|loc|excerpt",
        ]) == 0
        assert main([
            "--db", str(db), "ingest",
            "--text", "Claim B", "--source", "test|loc|excerpt",
        ]) == 0
        return db

    def test_cli_link_command(self, cli_db: Path):
        from memorymaster.cli import main

        result = main(["--db", str(cli_db), "link", "1", "2", "--type", "supports"])
        assert result == 0

    def test_cli_links_command(self, cli_db: Path):
        from memorymaster.cli import main

        main(["--db", str(cli_db), "link", "1", "2", "--type", "relates_to"])
        result = main(["--db", str(cli_db), "links", "1"])
        assert result == 0

    def test_cli_unlink_command(self, cli_db: Path):
        from memorymaster.cli import main

        main(["--db", str(cli_db), "link", "1", "2", "--type", "relates_to"])
        result = main(["--db", str(cli_db), "unlink", "1", "2"])
        assert result == 0

    def test_cli_link_json_output(self, cli_db: Path, capsys):
        from memorymaster.cli import main

        result = main(["--db", str(cli_db), "--json", "link", "1", "2", "--type", "derived_from"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["ok"] is True
        assert data["data"]["link_type"] == "derived_from"

    def test_cli_links_json_output(self, cli_db: Path, capsys):
        from memorymaster.cli import main

        main(["--db", str(cli_db), "link", "1", "2", "--type", "supports"])
        main(["--db", str(cli_db), "link", "1", "2", "--type", "contradicts"])

        capsys.readouterr()  # clear prior output
        result = main(["--db", str(cli_db), "--json", "links", "1"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["ok"] is True
        assert data["data"]["rows"] == 2
        assert len(data["data"]["links"]) == 2

    def test_cli_unlink_json_output(self, cli_db: Path, capsys):
        from memorymaster.cli import main

        main(["--db", str(cli_db), "link", "1", "2", "--type", "relates_to"])

        capsys.readouterr()
        result = main(["--db", str(cli_db), "--json", "unlink", "1", "2", "--type", "relates_to"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["ok"] is True
        assert data["data"]["removed"] == 1

    def test_cli_links_filter_by_type(self, cli_db: Path, capsys):
        from memorymaster.cli import main

        main(["--db", str(cli_db), "link", "1", "2", "--type", "supports"])
        main(["--db", str(cli_db), "link", "1", "2", "--type", "contradicts"])

        capsys.readouterr()
        result = main(["--db", str(cli_db), "--json", "links", "1", "--type", "supports"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["data"]["rows"] == 1
        assert data["data"]["links"][0]["link_type"] == "supports"


class TestClaimLinksIdempotentMigration:
    def test_init_db_twice_is_safe(self):
        """Calling init_db twice should not fail (CREATE TABLE IF NOT EXISTS)."""
        svc = _make_service()
        # Second init on the SAME service instance should be idempotent
        svc.init_db()

        a = _ingest_claim(svc, "Claim A")
        b = _ingest_claim(svc, "Claim B")
        link = svc.add_claim_link(a, b, "relates_to")
        assert link.id > 0
