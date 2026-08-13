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
from memorymaster.knowledge.graph_observation_engine import (
    GraphObservationEngine,
    review_observation_candidates,
)
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


def _fixture_claim(service: MemoryService, repository: CaptureRepository, evidence):
    claim = service.ingest(
        str(evidence.text or ""),
        [CitationInput(source="demo", locator=f"evidence:{evidence.id}")],
        scope="project:demo",
        confidence=0.8,
        source_agent="memorymaster-demo",
    )
    repository.link_claim_evidence(claim_id=claim.id, evidence_item_id=int(evidence.id))
    return claim


def _run_fixture_worker(service: MemoryService, repository: CaptureRepository, captures: list) -> tuple[list, int]:
    evidence = [
        service.list_evidence_items(source_item_id=int(receipt.source_item["id"]), limit=1)[0]
        for receipt in captures
        if receipt.evidence is not None
    ]
    claims = [_fixture_claim(service, repository, item) for item in evidence]
    with service.store.connect() as conn:
        conn.execute("UPDATE source_items SET sensitivity='none'")
        conn.execute("UPDATE evidence_items SET sensitivity='none'")
        conn.commit()
    jobs = repository.lease_jobs(owner="memorymaster-demo", stages=("extract_claims",), limit=100)
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


def _add_dependency_graph(service: MemoryService, repository: CaptureRepository, claims: list) -> None:
    with service.store.connect() as conn:
        for entity_id, name in ((10, "API"), (20, "Database"), (30, "Migration")):
            conn.execute(
                """INSERT INTO entities
                   (id, canonical_name, entity_type, scope, created_at, updated_at)
                   VALUES (?, ?, 'system', 'project:demo', ?, ?)""",
                (entity_id, name, "2026-08-12", "2026-08-12"),
            )
        conn.commit()
    for claim, edge in zip(
        (claims[0], claims[1], claims[1], claims[2]),
        ((10, 20), (10, 20), (20, 30), (20, 30)),
    ):
        repository.add_edge_support(
            source_entity_id=edge[0],
            target_entity_id=edge[1],
            relation="depends_on",
            supporting_claim_id=claim.id,
            scope="project:demo",
            ontology_version="personal-v1",
        )


def _run_observation_fixture(service: MemoryService, repository: CaptureRepository, claims: list) -> tuple[int, dict]:
    _add_dependency_graph(service, repository, claims)
    claim_ids = [claim.id for claim in claims]
    output = json.dumps(
        {
            "decision": "emit",
            "name": "Three-blocker dependency chain",
            "observation_type": "dependency",
            "summary": "Three blockers share the same API, database, and migration chain.",
            "assertions": [
                {
                    "text": "All three blockers are supported by the supplied chain.",
                    "supporting_claim_ids": claim_ids,
                }
            ],
        }
    )
    engine = GraphObservationEngine(service.store, llm_call=lambda _system, _prompt: output)
    engine.repo.queue_discovery(
        tenant_id=None,
        scope="project:demo",
        ontology_version="personal-v1",
        cycle_hour="2026-08-12T23",
    )
    engine.process_discovery(owner="memorymaster-demo", scope="project:demo")
    engine.process_synthesis(owner="memorymaster-demo", scope="project:demo")
    review_observation_candidates(service.store, scope="project:demo")
    row = engine.repo.scope_observations(scope="project:demo", tenant_id=None)[0]
    observation_id = int(row["observation_claim_id"])
    recalled = recall(
        "blocker dependency chain",
        scope_allowlist=["project:demo"],
        include_observations=True,
        observation_limit=2,
        retrieval_mode="legacy",
        db=service.store.db_path,
        workspace=service.workspace_root,
    )
    return observation_id, {
        "recall": recalled,
        "support": engine.repo.observation_support_rows(observation_id),
    }


def _demo_report(workspace: Path, db: Path, captures: list) -> dict:
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()
    repository = CaptureRepository(service.store)
    claims, completed = _run_fixture_worker(service, repository, captures)
    confirmed = [
        service.store.apply_status_transition(
            claim,
            to_status="confirmed",
            reason="deterministic demo fixture",
            event_type="validator",
        )
        for claim in claims
    ]
    paths = _extract_fixture_graph(db, confirmed[0])
    observation_id, observation = _run_observation_fixture(service, repository, confirmed)
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
        "promoted_claim_id": confirmed[0].id,
        "recall_claim_ids": [int(claim["claim_id"]) for claim in recalled.claims],
        "recall_citations": [citation for claim in recalled.claims for citation in claim["citations"]],
        "graph_paths": paths,
        "observation_claim_id": observation_id,
        "observation_recall": list(observation["recall"].observations),
        "observation_supports": observation["support"],
    }
    repository.retire_source(int(captures[0].source_item["id"]), reason="demo support retirement")
    report["observation_status_after_retirement"] = service.store.get_claim(observation_id).status
    del service, repository
    gc.collect()
    return report


def run_disposable_demo() -> dict:
    """Run the complete local demo and return a portable evidence report."""
    with tempfile.TemporaryDirectory(prefix="memorymaster-demo-") as raw:
        workspace = Path(raw)
        db = workspace / "demo.db"
        document = workspace / "project-note.md"
        document.write_text("Project Atlas uses governed evidence lineage.", encoding="utf-8")
        with _capture_environment(workspace):
            captures = [
                remember(
                    text="Blocker one: Alice needs the API before Project Atlas can ship.",
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
                remember(
                    text="Blocker three: the migration depends on the database rollout.",
                    scope="project:demo",
                    db=db,
                    workspace=workspace,
                ),
            ]
        return _demo_report(workspace, db, captures)
