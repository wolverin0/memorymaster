from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

from memorymaster.models import CitationInput
from memorymaster.service import MemoryService


def _case_db(prefix: str) -> Path:
    fd, raw = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".db", dir=".tmp_cases")
    os.close(fd)
    Path(raw).unlink(missing_ok=True)
    return Path(raw)


def _case_workspace(prefix: str) -> Path:
    base = Path(".tmp_pytest")
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=str(base)))


def _force_status(db: Path, claim_id: int, status: str, updated_at: str) -> None:
    con = sqlite3.connect(str(db))
    con.execute(
        "UPDATE claims SET status=?, updated_at=?, last_validated_at=? WHERE id=?",
        (status, updated_at, updated_at, claim_id),
    )
    con.commit()
    con.close()


def test_compaction_emits_traceability_artifacts_and_preserves_claim_citation_lineage() -> None:
    db = _case_db("sqlite-compaction-trace")
    workspace = _case_workspace("compaction-trace")
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()

    stale = service.ingest(
        text="Primary API endpoint is https://old.example.com/v1",
        citations=[
            CitationInput(source="session://chat", locator="turn-1", excerpt="old endpoint"),
            CitationInput(source="ticket://ops", locator="INC-1001", excerpt="legacy route"),
        ],
        subject="api",
        predicate="endpoint",
        object_value="https://old.example.com/v1",
    )
    conflicted = service.ingest(
        text="Release deadline is 2026-04-01",
        citations=[CitationInput(source="session://chat", locator="turn-2", excerpt="old date")],
        subject="release",
        predicate="deadline",
        object_value="2026-04-01",
    )

    _force_status(db, stale.id, "stale", "2025-01-01T00:00:00+00:00")
    _force_status(db, conflicted.id, "conflicted", "2025-01-01T00:00:00+00:00")

    compact_result = service.compact(retain_days=30, event_retain_days=36500)

    # Backward-compatible payload shape for callers relying on old keys.
    assert set(compact_result.keys()) == {"archived_claims", "deleted_events"}
    assert compact_result["archived_claims"] == 2

    artifacts_dir = workspace / "artifacts" / "compaction"
    summary_graph_path = artifacts_dir / "summary_graph.json"
    traceability_path = artifacts_dir / "traceability.json"
    assert summary_graph_path.exists()
    assert traceability_path.exists()

    summary_graph = json.loads(summary_graph_path.read_text(encoding="utf-8"))
    traceability = json.loads(traceability_path.read_text(encoding="utf-8"))

    assert summary_graph["artifact_type"] == "summary_graph"
    assert traceability["artifact_type"] == "traceability"
    assert summary_graph["run"]["archived_claims"] == 2
    assert traceability["run"]["archived_claims"] == 2

    claim_nodes = summary_graph["nodes"]["claims"]
    citation_nodes = summary_graph["nodes"]["citations"]
    claim_edges = [edge for edge in summary_graph["edges"] if edge["type"] == "claim_to_citation"]
    assert len(claim_nodes) == 2
    assert len(citation_nodes) == 3
    assert len(claim_edges) == 3
    assert all(node["status_after"] == "archived" for node in claim_nodes)
    assert {int(node["claim_id"]) for node in claim_nodes} == {stale.id, conflicted.id}

    lineage_by_claim = {int(row["claim_id"]): row for row in traceability["claim_lineage"]}
    assert set(lineage_by_claim.keys()) == {stale.id, conflicted.id}
    assert lineage_by_claim[stale.id]["status_before"] == "stale"
    assert lineage_by_claim[conflicted.id]["status_before"] == "conflicted"
    assert len(lineage_by_claim[stale.id]["citations"]) == 2
    assert len(lineage_by_claim[conflicted.id]["citations"]) == 1

    stale_after = service.store.get_claim(stale.id, include_citations=False)
    conflicted_after = service.store.get_claim(conflicted.id, include_citations=False)
    assert stale_after is not None and stale_after.status == "archived"
    assert conflicted_after is not None and conflicted_after.status == "archived"

    compaction_events = service.list_events(event_type="compaction_run", limit=5)
    assert compaction_events
    payload = json.loads(compaction_events[0].payload_json or "{}")
    assert payload["archived_claims"] == 2
    assert payload["retain_days"] == 30
    assert payload["event_retain_days"] == 36500
    assert payload["artifacts"]["summary_graph"].endswith("summary_graph.json")
    assert payload["artifacts"]["traceability"].endswith("traceability.json")


def test_compaction_writes_empty_trace_artifacts_when_no_candidates() -> None:
    db = _case_db("sqlite-compaction-trace-empty")
    workspace = _case_workspace("compaction-trace-empty")
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()

    claim = service.ingest(
        text="Support email is help@example.com",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="seed")],
        subject="support",
        predicate="email",
        object_value="help@example.com",
    )
    _force_status(db, claim.id, "confirmed", "2025-01-01T00:00:00+00:00")

    compact_result = service.compact(retain_days=30, event_retain_days=36500)
    assert set(compact_result.keys()) == {"archived_claims", "deleted_events"}
    assert compact_result["archived_claims"] == 0

    artifacts_dir = workspace / "artifacts" / "compaction"
    summary_graph = json.loads((artifacts_dir / "summary_graph.json").read_text(encoding="utf-8"))
    traceability = json.loads((artifacts_dir / "traceability.json").read_text(encoding="utf-8"))

    assert summary_graph["nodes"]["claims"] == []
    assert summary_graph["nodes"]["citations"] == []
    assert traceability["claim_lineage"] == []
    assert traceability["summary_to_source"] == []

