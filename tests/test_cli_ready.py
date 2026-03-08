"""Tests for the `memorymaster ready` CLI command."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from memorymaster.cli import main


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    db = tmp_path / "test_ready.db"
    main(["--db", str(db), "init-db"])
    return db


def _run(capsys, argv: list[str]) -> dict:
    """Run CLI and return {'rc': int, 'out': str}."""
    capsys.readouterr()
    rc = main(argv)
    out = capsys.readouterr().out.strip()
    return {"rc": rc, "out": out}


def _run_json(capsys, argv: list[str]) -> dict:
    """Run CLI with --json and return parsed JSON."""
    capsys.readouterr()
    rc = main(argv)
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    return {"rc": rc, **parsed}


class TestReadyEmpty:
    """Ready command on an empty database."""

    def test_ready_empty_text(self, tmp_db: Path, capsys) -> None:
        result = _run(capsys, ["--db", str(tmp_db), "ready"])
        assert result["rc"] == 0
        assert "All clear" in result["out"]

    def test_ready_empty_json(self, tmp_db: Path, capsys) -> None:
        result = _run_json(capsys, ["--json", "--db", str(tmp_db), "ready"])
        assert result["rc"] == 0
        assert result["ok"] is True
        assert result["data"]["total_attention"] == 0
        assert result["data"]["stale"]["count"] == 0
        assert result["data"]["conflicted"]["count"] == 0
        assert result["data"]["low_confidence"]["count"] == 0


class TestReadyWithClaims:
    """Ready command with claims that need attention."""

    def _ingest(self, db: Path, text: str, source: str = "test|loc|exc") -> int:
        """Ingest a claim and return its id."""
        from memorymaster.service import MemoryService
        svc = MemoryService(str(db))
        from memorymaster.models import CitationInput
        claim = svc.ingest(text, citations=[CitationInput(source="test", locator="loc", excerpt="exc")])
        return claim.id

    def test_ready_low_confidence_candidates(self, tmp_db: Path, capsys) -> None:
        """Candidates with default confidence (0.5) should show when threshold > 0.5."""
        # Ingest creates candidates with confidence=0.5
        self._ingest(tmp_db, "Low confidence claim one")
        self._ingest(tmp_db, "Low confidence claim two")

        # Default threshold is 0.5, so 0.5 is NOT < 0.5 -> should not appear
        result = _run_json(capsys, ["--json", "--db", str(tmp_db), "ready"])
        assert result["data"]["low_confidence"]["count"] == 0

        # With threshold=0.6, confidence 0.5 IS < 0.6 -> should appear
        result2 = _run_json(capsys, [
            "--json", "--db", str(tmp_db),
            "ready", "--confidence-threshold", "0.6",
        ])
        assert result2["data"]["low_confidence"]["count"] == 2

    def test_ready_limit(self, tmp_db: Path, capsys) -> None:
        """--limit caps the number of claims per category."""
        for i in range(5):
            self._ingest(tmp_db, f"Candidate claim {i}")

        result = _run_json(capsys, [
            "--json", "--db", str(tmp_db),
            "ready", "--confidence-threshold", "0.6", "--limit", "2",
        ])
        assert len(result["data"]["low_confidence"]["claims"]) <= 2

    def test_ready_text_output_shows_suggestions(self, tmp_db: Path, capsys) -> None:
        """Text output should include actionable suggestions."""
        self._ingest(tmp_db, "Some candidate claim")

        result = _run(capsys, [
            "--db", str(tmp_db),
            "ready", "--confidence-threshold", "0.6",
        ])
        assert result["rc"] == 0
        assert "need attention" in result["out"]
        assert "run-cycle" in result["out"].lower() or "run-cycle" in result["out"]
