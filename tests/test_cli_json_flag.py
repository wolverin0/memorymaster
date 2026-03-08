"""Tests for the --json / -j CLI flag (P2 feature #13)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from memorymaster.cli import main


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    db = tmp_path / "test_json.db"
    main(["--db", str(db), "init-db"])
    return db


def _capture(capsys, argv: list[str]) -> dict:
    """Run CLI with --json and return parsed JSON output."""
    # Clear any prior captured output
    capsys.readouterr()
    rc = main(argv)
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    return {"rc": rc, **parsed}


class TestJsonFlagEnvelope:
    """Verify the JSON envelope schema across commands."""

    def test_init_db_json(self, tmp_path: Path, capsys) -> None:
        db = tmp_path / "init_test.db"
        result = _capture(capsys, ["--json", "--db", str(db), "init-db"])
        assert result["rc"] == 0
        assert result["ok"] is True
        assert "db" in result["data"]
        assert "query_ms" in result["meta"]
        assert isinstance(result["meta"]["query_ms"], (int, float))

    def test_ingest_json(self, tmp_db: Path, capsys) -> None:
        result = _capture(capsys, [
            "--json", "--db", str(tmp_db),
            "ingest", "--text", "The sky is blue",
            "--source", "observation|sky|looked up",
        ])
        assert result["rc"] == 0
        assert result["ok"] is True
        assert result["data"]["id"] == 1
        assert result["data"]["status"] == "candidate"
        assert result["data"]["text"] == "The sky is blue"
        assert result["meta"]["total"] == 1
        assert result["meta"]["query_ms"] >= 0

    def test_list_claims_json(self, tmp_db: Path, capsys) -> None:
        main(["--db", str(tmp_db), "ingest", "--text", "Claim A", "--source", "s1"])
        main(["--db", str(tmp_db), "ingest", "--text", "Claim B", "--source", "s2"])
        result = _capture(capsys, ["--json", "--db", str(tmp_db), "list-claims"])
        assert result["rc"] == 0
        assert result["ok"] is True
        assert result["meta"]["total"] == 2
        assert len(result["data"]) == 2
        texts = {c["text"] for c in result["data"]}
        assert "Claim A" in texts
        assert "Claim B" in texts

    def test_list_events_json(self, tmp_db: Path, capsys) -> None:
        main(["--db", str(tmp_db), "ingest", "--text", "Event test", "--source", "s"])
        result = _capture(capsys, ["--json", "--db", str(tmp_db), "list-events"])
        assert result["rc"] == 0
        assert result["ok"] is True
        assert result["meta"]["total"] >= 1
        assert isinstance(result["data"], list)

    def test_query_json_empty(self, tmp_db: Path, capsys) -> None:
        result = _capture(capsys, [
            "--json", "--db", str(tmp_db),
            "query", "nonexistent term",
        ])
        assert result["rc"] == 0
        assert result["ok"] is True
        assert result["data"] == []
        assert result["meta"]["total"] == 0

    def test_query_json_with_results(self, tmp_db: Path, capsys) -> None:
        main(["--db", str(tmp_db), "ingest", "--text", "Query target claim", "--source", "s"])
        # Confirm the claim so query finds it (query excludes candidates by default)
        from memorymaster.lifecycle import transition_claim
        from memorymaster.service import MemoryService
        svc = MemoryService(str(tmp_db), workspace_root=Path.cwd())
        transition_claim(svc.store, 1, "confirmed", reason="test", event_type="validator")

        result = _capture(capsys, [
            "--json", "--db", str(tmp_db),
            "query", "target",
        ])
        assert result["rc"] == 0
        assert result["ok"] is True
        assert result["meta"]["total"] >= 1
        row = result["data"][0]
        assert "claim" in row
        assert "score" in row
        assert row["claim"]["text"] == "Query target claim"

    def test_run_cycle_json(self, tmp_db: Path, capsys) -> None:
        result = _capture(capsys, ["--json", "--db", str(tmp_db), "run-cycle"])
        assert result["rc"] == 0
        assert result["ok"] is True
        assert "query_ms" in result["meta"]


class TestJsonFlagDefault:
    """Verify that omitting --json keeps human-readable output."""

    def test_ingest_human_readable(self, tmp_db: Path, capsys) -> None:
        capsys.readouterr()
        rc = main(["--db", str(tmp_db), "ingest", "--text", "Human output", "--source", "s"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "ingested claim_id=" in out
        # Should NOT be valid JSON envelope
        assert '"ok"' not in out

    def test_list_claims_human_readable(self, tmp_db: Path, capsys) -> None:
        main(["--db", str(tmp_db), "ingest", "--text", "Test claim", "--source", "s"])
        capsys.readouterr()
        rc = main(["--db", str(tmp_db), "list-claims"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "rows=" in out


class TestJsonFlagError:
    """Verify error envelope when --json is set and command fails."""

    def test_error_json_envelope(self, tmp_path: Path, capsys) -> None:
        db = tmp_path / "missing.db"
        result = _capture(capsys, ["--json", "--db", str(db), "list-claims"])
        assert result["rc"] == 2
        assert result["ok"] is False
        assert "error" in result


class TestJsonShortFlag:
    """Verify -j works as alias for --json."""

    def test_short_flag(self, tmp_db: Path, capsys) -> None:
        result = _capture(capsys, ["-j", "--db", str(tmp_db), "list-claims"])
        assert result["rc"] == 0
        assert result["ok"] is True
