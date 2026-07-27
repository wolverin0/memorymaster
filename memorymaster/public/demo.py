"""Disposable deterministic demonstration of the public v1 memory lifecycle.

The demo captures text and Markdown into a temporary SQLite database, runs a
local fixture worker, promotes one governed claim, recalls it with citations,
and shows its supported graph relationship. It performs no network calls and
deletes the temporary database on exit.
"""

from __future__ import annotations

import json
import os
import tempfile
import gc
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from memorymaster.capture import CaptureRepository
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.knowledge.entity_graph import EntityGraph
from memorymaster.public.v1 import recall, remember


@contextmanager
def _capture_environment(workspace: Path):
    names = ("MEMORYMASTER_CAPTURE_ROOTS", "MEMORYMASTER_CAPTURE_TRUST_MODE")
    previous = {name: os.environ.get(name) for name in names}
    os.environ["MEMORYMASTER_CAPTURE_ROOTS"] = f"demo={workspace}"
    os.environ["MEMORYMASTER_CAPTURE_TRUST_MODE"] = "local-trusted"
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _fixture_claim(
    service: MemoryService, repository: CaptureRepository, evidence
):
    claim = service.ingest(
        str(evidence.text or ""),
        [CitationInput(source="demo", locator=f"evidence:{evidence.id}")],
        scope="project:demo",
        source_agent="memorymaster-demo",
    )
    repository.link_claim_evidence(
        claim_id=claim.id, evidence_item_id=int(evidence.id)
    )
    return claim


def _run_fixture_worker(
    service: MemoryService, repository: CaptureRepository, captures: list
) -> tuple[list, int]:
    evidence = [
        service.list_evidence_items(
            source_item_id=int(receipt.source_item["id"]), limit=1
        )[0]
        for receipt in captures
        if receipt.evidence is not None
    ]
    claims = [_fixture_claim(service, repository, item) for item in evidence]
    jobs = repository.lease_jobs(
        owner="memorymaster-demo", stages=("extract_claims",), limit=100
    )
    for job in jobs:
        repository.finish_job(job.id, status="completed")
    return claims, len(jobs)


def _extract_fixture_graph(db: Path, claim) -> list[dict]:
    payload = json.dumps(
        {
            "entities": [
                {"name": "Alice", "type": "person", "aliases": []},
                {"name": "Project Atlas", "type": "project", "aliases": []},
            ],
            "relations": [
                {
                    "source": "Alice",
                    "target": "Project Atlas",
                    "relation": "participates_in",
                }
            ],
        }
    )
    graph = EntityGraph(str(db))
    with patch("memorymaster.knowledge.entity_graph._llm_chat", return_value=payload):
        graph.extract_and_link(claim.id, claim.text)
    conn = graph._connect()
    try:
        rows = conn.execute(
            """SELECT se.canonical_name AS source, es.relation,
                      te.canonical_name AS target, es.supporting_claim_id
               FROM entity_edge_supports es
               JOIN entities se ON se.id=es.source_entity_id
               JOIN entities te ON te.id=es.target_entity_id
               WHERE es.supporting_claim_id=?""",
            (claim.id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def run_disposable_demo() -> dict:
    """Run the complete local demo and return a portable evidence report."""
    with tempfile.TemporaryDirectory(prefix="memorymaster-demo-") as raw:
        workspace = Path(raw)
        db = workspace / "demo.db"
        document = workspace / "project-note.md"
        document.write_text(
            "Project Atlas uses governed evidence lineage.", encoding="utf-8"
        )
        with _capture_environment(workspace):
            captures = [
                remember(
                    text="Alice participates in Project Atlas.",
                    scope="project:demo",
                    db=db,
                    workspace=workspace,
                ),
                remember(
                    path=document,
                    scope="project:demo",
                    db=db,
                    workspace=workspace,
                ),
            ]
        service = MemoryService(db, workspace_root=workspace)
        service.init_db()
        repository = CaptureRepository(service.store)
        claims, completed = _run_fixture_worker(service, repository, captures)
        confirmed = service.store.apply_status_transition(
            claims[0],
            to_status="confirmed",
            reason="deterministic demo fixture",
            event_type="validator",
        )
        paths = _extract_fixture_graph(db, confirmed)
        recalled = recall(
            "Alice Project Atlas",
            scope_allowlist=["project:demo"],
            db=db,
            workspace=workspace,
        )
        report = {
            "api_version": recalled.api_version,
            "temporary_database_disposed": True,
            "captures": len(captures),
            "fixture_jobs_completed": completed,
            "candidate_claims_created": len(claims),
            "promoted_claim_id": confirmed.id,
            "recall_claim_ids": [
                int(claim["claim_id"]) for claim in recalled.claims
            ],
            "recall_citations": [
                citation
                for claim in recalled.claims
                for citation in claim["citations"]
            ],
            "graph_paths": paths,
        }
        del service, repository
        gc.collect()
        return report
