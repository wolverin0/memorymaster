from __future__ import annotations

from pathlib import Path

from memorymaster.surfaces.mcp_server import QueryMetaDecisionsInput, _validate_tool_input
from memorymaster.models import CitationInput
from memorymaster.service import MemoryService


def _service(tmp_path: Path) -> MemoryService:
    svc = MemoryService(db_target=tmp_path / "memory.db", workspace_root=tmp_path)
    svc.init_db()
    return svc


def _cite() -> list[CitationInput]:
    return [CitationInput(source="test-meta-decisions")]


def test_query_meta_decisions_groups_shared_subject_across_project_scopes(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    first = svc.ingest(
        "MemoryMaster keeps SQLite in WAL mode for concurrent agents.",
        citations=_cite(),
        claim_type="decision",
        subject="SQLite + WAL",
        scope="project:memorymaster",
    )
    second = svc.ingest(
        "Wezbridge also standardizes on SQLite WAL for local coordination state.",
        citations=_cite(),
        claim_type="architecture",
        subject="SQLite + WAL",
        scope="project:wezbridge",
    )
    svc.ingest(
        "Global SQLite WAL note is not project scoped.",
        citations=_cite(),
        claim_type="decision",
        subject="SQLite + WAL",
        scope="global",
    )
    svc.ingest(
        "Fact claims are ignored by the default meta-decision filter.",
        citations=_cite(),
        claim_type="fact",
        subject="SQLite + WAL",
        scope="project:other",
    )

    result = svc.query_meta_decisions("sqlite wal")

    assert len(result["groups"]) == 1
    group = result["groups"][0]
    assert group["concept"] == "SQLite + WAL"
    assert group["claim_count"] == 2
    assert group["scopes"] == ["project:memorymaster", "project:wezbridge"]
    assert set(group["exemplar_claim_ids"]) == {first.id, second.id}


def test_query_meta_decisions_clusters_by_keyword_overlap_without_subject(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    first = svc.ingest(
        "Use SQLite WAL for concurrent local writes.",
        citations=_cite(),
        claim_type="decision",
        scope="project:memorymaster",
    )
    second = svc.ingest(
        "SQLite WAL prevents DB corruption during parallel agent access.",
        citations=_cite(),
        claim_type="architecture",
        scope="project:wezbridge",
    )

    result = svc.query_meta_decisions("sqlite wal")
    groups = result["groups"]

    assert len(groups) == 1
    assert groups[0]["claim_count"] == 2
    assert groups[0]["scopes"] == ["project:memorymaster", "project:wezbridge"]
    assert set(groups[0]["exemplar_claim_ids"]) == {first.id, second.id}


def test_query_meta_decisions_respects_claim_types_and_top_n(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    svc.ingest(
        "OAuth decision for app authentication.",
        citations=_cite(),
        claim_type="decision",
        subject="OAuth",
        scope="project:alpha",
    )
    constraint = svc.ingest(
        "Queue constraint for workers.",
        citations=_cite(),
        claim_type="constraint",
        subject="Queue",
        scope="project:beta",
    )

    result = svc.query_meta_decisions("", claim_types=["constraint"], top_n=1)

    assert result["groups"] == [
        {
            "concept": "Queue",
            "claim_count": 1,
            "scopes": ["project:beta"],
            "exemplar_claim_ids": [constraint.id],
        }
    ]


def test_query_meta_decisions_input_model_rejects_extra_fields() -> None:
    result = _validate_tool_input(
        QueryMetaDecisionsInput,
        {
            "query": "sqlite wal",
            "unknown": True,
        },
    )

    assert isinstance(result, dict)
    assert result["code"] == "VALIDATION_ERROR"
    assert result["field"] == "unknown"
