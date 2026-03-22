"""Tests for claim staleness detection (memorymaster.jobs.staleness)."""

from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


from memorymaster.models import CitationInput
from memorymaster.service import MemoryService
from memorymaster.jobs.staleness import (
    StalenessResult,
    check_claim_staleness,
    run,
    _extract_file_paths,
    _file_mtime_utc,
)


def _case_db(prefix: str) -> Path:
    fd, raw = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".db", dir=".tmp_cases")
    os.close(fd)
    Path(raw).unlink(missing_ok=True)
    return Path(raw)


def _make_service(workspace: Path | None = None) -> MemoryService:
    db = _case_db("staleness")
    service = MemoryService(db, workspace_root=workspace or Path.cwd())
    service.init_db()
    return service


def _ingest_with_file_citation(
    service: MemoryService,
    text: str,
    source_file: str,
    *,
    status: str = "confirmed",
) -> int:
    claim = service.ingest(
        text=text,
        citations=[CitationInput(source=source_file, locator="line:1")],
    )
    if status == "confirmed":
        from memorymaster.lifecycle import transition_claim
        transition_claim(
            service.store,
            claim.id,
            to_status="confirmed",
            reason="test setup",
            event_type="transition",
        )
    return claim.id


class TestExtractFilePaths:
    def test_relative_path(self, tmp_path):
        from memorymaster.models import Claim, Citation
        claim = Claim(
            id=1, text="x", idempotency_key=None, normalized_text=None,
            claim_type=None, subject=None, predicate=None, object_value=None,
            scope="project", volatility="medium", status="confirmed",
            confidence=0.8, pinned=False, supersedes_claim_id=None,
            replaced_by_claim_id=None, created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00", last_validated_at=None,
            archived_at=None,
            citations=[
                Citation(id=1, claim_id=1, source="src/main.py", locator=None,
                         excerpt=None, created_at="2025-01-01T00:00:00+00:00"),
            ],
        )
        paths = _extract_file_paths(claim, tmp_path)
        assert len(paths) == 1
        assert paths[0] == tmp_path / "src/main.py"

    def test_url_ignored(self, tmp_path):
        from memorymaster.models import Claim, Citation
        claim = Claim(
            id=1, text="x", idempotency_key=None, normalized_text=None,
            claim_type=None, subject=None, predicate=None, object_value=None,
            scope="project", volatility="medium", status="confirmed",
            confidence=0.8, pinned=False, supersedes_claim_id=None,
            replaced_by_claim_id=None, created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00", last_validated_at=None,
            archived_at=None,
            citations=[
                Citation(id=1, claim_id=1, source="https://example.com/doc",
                         locator=None, excerpt=None,
                         created_at="2025-01-01T00:00:00+00:00"),
            ],
        )
        paths = _extract_file_paths(claim, tmp_path)
        assert paths == []

    def test_plain_label_ignored(self, tmp_path):
        from memorymaster.models import Claim, Citation
        claim = Claim(
            id=1, text="x", idempotency_key=None, normalized_text=None,
            claim_type=None, subject=None, predicate=None, object_value=None,
            scope="project", volatility="medium", status="confirmed",
            confidence=0.8, pinned=False, supersedes_claim_id=None,
            replaced_by_claim_id=None, created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00", last_validated_at=None,
            archived_at=None,
            citations=[
                Citation(id=1, claim_id=1, source="team discussion",
                         locator=None, excerpt=None,
                         created_at="2025-01-01T00:00:00+00:00"),
            ],
        )
        paths = _extract_file_paths(claim, tmp_path)
        assert paths == []


class TestFileMtimeUtc:
    def test_existing_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        mtime = _file_mtime_utc(f)
        assert mtime is not None
        assert mtime.tzinfo is not None

    def test_missing_file(self, tmp_path):
        f = tmp_path / "nonexistent.txt"
        mtime = _file_mtime_utc(f)
        assert mtime is None


class TestCheckClaimStaleness:
    def test_stale_when_file_modified_after_validation(self, tmp_path):
        source_file = tmp_path / "config.py"
        source_file.write_text("old content")

        from memorymaster.models import Claim, Citation
        # Set last_validated_at to the past
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        claim = Claim(
            id=1, text="config uses X", idempotency_key=None,
            normalized_text=None, claim_type=None, subject=None,
            predicate=None, object_value=None, scope="project",
            volatility="medium", status="confirmed", confidence=0.8,
            pinned=False, supersedes_claim_id=None, replaced_by_claim_id=None,
            created_at=past, updated_at=past, last_validated_at=past,
            archived_at=None,
            citations=[
                Citation(id=1, claim_id=1, source="config.py", locator=None,
                         excerpt=None, created_at=past),
            ],
        )

        # Touch file to make it newer than validation
        time.sleep(0.05)
        source_file.write_text("new content")

        is_stale, changed = check_claim_staleness(claim, tmp_path, mode="mtime")
        assert is_stale is True
        assert len(changed) == 1

    def test_not_stale_when_file_older(self, tmp_path):
        source_file = tmp_path / "stable.py"
        source_file.write_text("stable content")

        from memorymaster.models import Claim, Citation
        # Set last_validated_at to the future (simulates recent validation)
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        claim = Claim(
            id=1, text="stable uses Y", idempotency_key=None,
            normalized_text=None, claim_type=None, subject=None,
            predicate=None, object_value=None, scope="project",
            volatility="medium", status="confirmed", confidence=0.8,
            pinned=False, supersedes_claim_id=None, replaced_by_claim_id=None,
            created_at=future, updated_at=future, last_validated_at=future,
            archived_at=None,
            citations=[
                Citation(id=1, claim_id=1, source="stable.py", locator=None,
                         excerpt=None, created_at=future),
            ],
        )

        is_stale, changed = check_claim_staleness(claim, tmp_path, mode="mtime")
        assert is_stale is False
        assert changed == []

    def test_no_citations(self, tmp_path):
        from memorymaster.models import Claim
        claim = Claim(
            id=1, text="no refs", idempotency_key=None,
            normalized_text=None, claim_type=None, subject=None,
            predicate=None, object_value=None, scope="project",
            volatility="medium", status="confirmed", confidence=0.8,
            pinned=False, supersedes_claim_id=None, replaced_by_claim_id=None,
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
            last_validated_at="2025-01-01T00:00:00+00:00",
            archived_at=None, citations=[],
        )
        is_stale, changed = check_claim_staleness(claim, tmp_path, mode="mtime")
        assert is_stale is False


class TestRunStaleness:
    def test_detects_and_transitions_stale_claim(self, tmp_path):
        svc = _make_service(workspace=tmp_path)

        # Create a source file
        src = tmp_path / "module.py"
        src.write_text("original")

        # Ingest a claim citing that file
        claim_id = _ingest_with_file_citation(
            svc, "module.py exports foo", "module.py", status="confirmed",
        )

        # Simulate file change after claim validation
        time.sleep(0.05)
        src.write_text("modified")

        result = run(svc.store, tmp_path, mode="mtime", dry_run=False)
        assert result.stale_detected >= 1

        # Verify claim is now stale
        claim = svc.store.get_claim(claim_id)
        assert claim.status == "stale"

    def test_dry_run_does_not_transition(self, tmp_path):
        svc = _make_service(workspace=tmp_path)

        src = tmp_path / "lib.py"
        src.write_text("original")

        claim_id = _ingest_with_file_citation(
            svc, "lib.py defines bar", "lib.py", status="confirmed",
        )

        time.sleep(0.05)
        src.write_text("modified")

        result = run(svc.store, tmp_path, mode="mtime", dry_run=True)
        assert result.stale_detected >= 1

        # Claim should NOT have transitioned
        claim = svc.store.get_claim(claim_id)
        assert claim.status == "confirmed"

    def test_pinned_claims_skipped(self, tmp_path):
        svc = _make_service(workspace=tmp_path)

        src = tmp_path / "pinned.py"
        src.write_text("original")

        claim_id = _ingest_with_file_citation(
            svc, "pinned.py stuff", "pinned.py", status="confirmed",
        )
        svc.store.set_pinned(claim_id, True, reason="test pin")

        time.sleep(0.05)
        src.write_text("modified")

        result = run(svc.store, tmp_path, mode="mtime", dry_run=False)
        assert result.skipped_pinned >= 1

        claim = svc.store.get_claim(claim_id)
        assert claim.status == "confirmed"

    def test_url_citations_not_flagged(self, tmp_path):
        svc = _make_service(workspace=tmp_path)

        claim = svc.ingest(
            text="External docs say X",
            citations=[CitationInput(source="https://docs.example.com/api")],
        )
        from memorymaster.lifecycle import transition_claim
        transition_claim(
            svc.store, claim.id, to_status="confirmed",
            reason="test", event_type="transition",
        )

        result = run(svc.store, tmp_path, mode="mtime", dry_run=False)
        refreshed = svc.store.get_claim(claim.id)
        assert refreshed.status == "confirmed"

    def test_result_dataclass_fields(self):
        r = StalenessResult()
        assert r.scanned == 0
        assert r.stale_detected == 0
        assert r.already_stale == 0
        assert r.skipped_no_citations == 0
        assert r.skipped_pinned == 0
        assert r.details == []


class TestCheckStalenessCLI:
    def test_cli_check_staleness_dry_run(self, tmp_path):
        from memorymaster.cli import main

        db_path = tmp_path / "test.db"
        # Init DB first
        ret = main(["--db", str(db_path), "init-db"])
        assert ret == 0

        # Run check-staleness with dry-run
        ret = main([
            "--db", str(db_path),
            "--workspace", str(tmp_path),
            "check-staleness",
            "--dry-run",
            "--mode", "mtime",
        ])
        assert ret == 0

    def test_cli_check_staleness_json(self, tmp_path, capsys):
        from memorymaster.cli import main

        db_path = tmp_path / "test.db"
        main(["--db", str(db_path), "init-db"])
        # Discard init-db output
        capsys.readouterr()

        ret = main([
            "--db", str(db_path),
            "--workspace", str(tmp_path),
            "--json",
            "check-staleness",
            "--dry-run",
        ])
        assert ret == 0
        captured = capsys.readouterr()
        import json
        data = json.loads(captured.out)
        assert data["ok"] is True
        assert "scanned" in data["data"]
