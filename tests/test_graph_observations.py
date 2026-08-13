from __future__ import annotations

import hashlib
import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from memorymaster.core.service import MemoryService
from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.dreaming.worker import DreamWorker
from memorymaster.evaluation.graph_observation_evaluator import evaluate_corpus
from memorymaster.capture.repository import CaptureRepository
from memorymaster.knowledge.graph_observation_engine import (
    GraphObservationEngine,
    review_observation_candidates,
)
from memorymaster.knowledge.graph_observation_repository import (
    GraphObservationRepository,
)
from memorymaster.public.v1 import recall as public_recall
from memorymaster.knowledge.graph_observations import (
    ObservationOutputError,
    ObservationSupport,
    canonical_signature,
    discover_components,
    parse_synthesis_output,
)
from memorymaster.surfaces.graph_observations_dashboard import (
    graph_observations_payload,
    hydrate_dashboard_html,
)


def _support(
    claim_id: int,
    evidence_id: int,
    source_item_id: int,
    source_entity_id: int,
    relation: str,
    target_entity_id: int,
    *,
    scope: str = "project:test",
    tenant_id: str | None = "tenant-a",
    confidence: float = 0.8,
) -> ObservationSupport:
    return ObservationSupport(
        claim_id=claim_id,
        evidence_id=evidence_id,
        source_item_id=source_item_id,
        source_entity_id=source_entity_id,
        relation=relation,
        target_entity_id=target_entity_id,
        ontology_version="personal-v1",
        scope=scope,
        tenant_id=tenant_id,
        confidence=confidence,
        occurred_at=f"2026-08-{evidence_id:02d}T00:00:00+00:00",
    )


def _eligible_supports() -> list[ObservationSupport]:
    return [
        _support(1, 1, 101, 10, "depends_on", 20),
        _support(2, 2, 102, 10, "depends_on", 20),
        _support(2, 2, 102, 20, "depends_on", 30),
        _support(3, 1, 101, 20, "depends_on", 30),
    ]


def test_deterministic_component_and_fingerprint_replay() -> None:
    rows = _eligible_supports()
    first = discover_components(rows, scope="project:test", tenant_id="tenant-a")
    second = discover_components(reversed(rows), scope="project:test", tenant_id="tenant-a")

    assert len(first.components) == 1
    assert first.components == second.components
    assert first.components[0].claim_ids == (1, 2, 3)
    assert first.components[0].evidence_ids == (1, 2)
    assert len(first.components[0].signatures) == 2


def test_symmetric_relation_canonicalization_is_exact() -> None:
    symmetric = frozenset({"related_to"})
    left = canonical_signature(9, "related_to", 2, "personal-v1", symmetric_relations=symmetric)
    right = canonical_signature(2, "related_to", 9, "personal-v1", symmetric_relations=symmetric)
    directed = canonical_signature(9, "depends_on", 2, "personal-v1", symmetric_relations=symmetric)

    assert left == right == (2, "related_to", 9, "personal-v1")
    assert directed == (9, "depends_on", 2, "personal-v1")


def test_scope_and_tenant_never_cross_components() -> None:
    rows = _eligible_supports()
    rows.extend(
        _support(
            row.claim_id + 10,
            row.evidence_id + 10,
            row.source_item_id + 10,
            row.source_entity_id,
            row.relation,
            row.target_entity_id,
            scope="project:other",
            tenant_id="tenant-b",
        )
        for row in _eligible_supports()
    )

    result = discover_components(rows, scope="project:test", tenant_id="tenant-a")

    assert len(result.components) == 1
    assert result.components[0].claim_ids == (1, 2, 3)


def test_hubs_shared_by_more_than_twenty_episodes_are_suppressed() -> None:
    rows = [_support(index, index, 100 + index, 1, "related_to", 2) for index in range(1, 22)]

    result = discover_components(rows, scope="project:test", tenant_id="tenant-a")

    assert result.components == ()
    assert any(item.code == "hub_signature_suppressed" for item in result.diagnostics)


def test_oversized_component_produces_diagnostic_not_synthesis() -> None:
    rows: list[ObservationSupport] = []
    for index in range(1, 22):
        if index <= 20:
            rows.append(_support(index, index, 100 + index, 1, "depends_on", 2))
        rows.append(_support(index, index, 100 + index, index + 2, "uses", index + 30))
    rows.extend(
        [
            _support(20, 20, 120, 50, "depends_on", 51),
            _support(21, 21, 121, 50, "depends_on", 51),
        ]
    )

    result = discover_components(rows, scope="project:test", tenant_id="tenant-a")

    assert result.components == ()
    assert any(item.code == "component_oversized" for item in result.diagnostics)


def test_synthesis_output_is_evidence_bound_and_strict() -> None:
    payload = json.dumps(
        {
            "decision": "emit",
            "name": "Three blockers share one dependency chain",
            "observation_type": "dependency",
            "summary": "The three confirmed blockers depend on the same two systems.",
            "assertions": [{"text": "The chain is supported by all three claims.", "supporting_claim_ids": [1, 2, 3]}],
        }
    )

    draft = parse_synthesis_output(payload, allowed_claim_ids={1, 2, 3})

    assert draft.decision == "emit"
    assert draft.assertions[0].supporting_claim_ids == (1, 2, 3)


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        json.dumps({"decision": "emit"}),
        json.dumps({"decision": "no_signal", "summary": "extra"}),
        json.dumps(
            {
                "decision": "emit",
                "name": "Unsupported",
                "observation_type": "dependency",
                "summary": "Unsupported claim.",
                "assertions": [{"text": "Bad", "supporting_claim_ids": [99]}],
            }
        ),
        json.dumps(
            {
                "decision": "emit",
                "name": "Boolean ID",
                "observation_type": "dependency",
                "summary": "Boolean IDs are never claim IDs.",
                "assertions": [{"text": "Bad", "supporting_claim_ids": [True]}],
            }
        ),
    ],
)
def test_synthesis_output_fails_closed(payload: str) -> None:
    with pytest.raises(ObservationOutputError):
        parse_synthesis_output(payload, allowed_claim_ids={1, 2, 3})


def test_migration_0020_is_sqlite_only_and_idempotent(tmp_path) -> None:
    service = MemoryService(tmp_path / "observations.db", workspace_root=tmp_path)
    service.init_db()
    service.init_db()

    with service.store.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                """SELECT name FROM sqlite_master WHERE type='table'
                   AND name LIKE 'graph_observation%'"""
            )
        }
        versions = conn.execute("SELECT COUNT(*) FROM schema_versions WHERE version=20").fetchone()[0]

    migration = importlib.import_module("memorymaster.stores.migrations.0020_graph_observations")
    with pytest.raises(RuntimeError, match="SQLite-only"):
        migration.apply_postgres(object())
    assert tables == {
        "graph_observations",
        "graph_observation_supports",
        "graph_observation_jobs",
    }
    assert versions == 1


def test_job_replay_and_expired_lease_recovery(tmp_path) -> None:
    service = MemoryService(tmp_path / "jobs.db", workspace_root=tmp_path)
    service.init_db()
    repo = GraphObservationRepository(service.store)
    digest = hashlib.sha256(b"component").hexdigest()
    first, created_first = repo.queue_job(
        tenant_id=None,
        scope="project:test",
        stage="synthesize",
        content_hash=digest,
        support_hash=digest,
        ontology_version="personal-v1",
        support_manifest=[[1, 2, 3, 4, "depends_on", 5, "personal-v1"]],
    )
    second, created_second = repo.queue_job(
        tenant_id=None,
        scope="project:test",
        stage="synthesize",
        content_hash=digest,
        support_hash=digest,
        ontology_version="personal-v1",
        support_manifest=[[1, 2, 3, 4, "depends_on", 5, "personal-v1"]],
    )
    leased = repo.lease_jobs(owner="worker-a", limit=1)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with service.store.connect() as conn:
        conn.execute(
            "UPDATE graph_observation_jobs SET lease_expires_at=? WHERE id=?",
            (past, first.id),
        )
        conn.commit()

    recovered = repo.lease_jobs(owner="worker-b", limit=1)

    assert created_first is True and created_second is False
    assert first.id == second.id == leased[0].id == recovered[0].id
    assert recovered[0].attempts == 2


def _graph_fixture(tmp_path):
    service = MemoryService(tmp_path / "lifecycle.db", workspace_root=tmp_path)
    service.init_db()
    store = service.store
    capture = CaptureRepository(store)
    external = store.upsert_external_source(source_type="direct", display_name="fixture")
    sources = []
    evidence = []
    for index in (1, 2):
        source = store.upsert_source_item(
            source_id=external.id,
            source_item_id=f"source-{index}",
            item_type="text",
            occurred_at=f"2026-08-0{index}T00:00:00+00:00",
            text=f"Dependency evidence {index}",
            content_hash=hashlib.sha256(f"source-{index}".encode()).hexdigest(),
            sensitivity="none",
        )
        sources.append(source)
        evidence.append(
            store.add_evidence_item(
                source_item_id=source.id,
                evidence_type="text",
                text=source.text,
                sensitivity="none",
                content_hash=source.content_hash,
            )
        )
    claims = []
    for index, evidence_index in ((1, 0), (2, 1), (3, 0)):
        claim = store.create_claim(
            text=f"Confirmed blocker {index}",
            citations=[CitationInput(source="fixture", locator=f"evidence:{evidence[evidence_index].id}")],
            claim_type="fact",
            scope="project:test",
            confidence=0.8,
            source_agent="fixture",
        )
        transition_claim(store, claim.id, "confirmed", "fixture")
        capture.link_claim_evidence(claim_id=claim.id, evidence_item_id=evidence[evidence_index].id)
        claims.append(claim)
    with store.connect() as conn:
        for entity_id in (10, 20, 30):
            conn.execute(
                """INSERT INTO entities
                   (id, canonical_name, entity_type, scope, created_at, updated_at)
                   VALUES (?, ?, 'system', 'project:test', ?, ?)""",
                (entity_id, f"system-{entity_id}", "2026-08-01", "2026-08-01"),
            )
        conn.commit()
    for claim, edge in zip(
        (claims[0], claims[1], claims[1], claims[2]),
        ((10, 20), (10, 20), (20, 30), (20, 30)),
    ):
        capture.add_edge_support(
            source_entity_id=edge[0],
            target_entity_id=edge[1],
            relation="depends_on",
            supporting_claim_id=claim.id,
            scope="project:test",
            ontology_version="personal-v1",
        )
    return service, capture, sources


def test_candidate_steward_promotion_and_retirement_staleness(tmp_path) -> None:
    service, capture, sources = _graph_fixture(tmp_path)
    baseline = public_recall(
        "blocker dependency",
        scope_allowlist=["project:test"],
        retrieval_mode="legacy",
        db=service.store.db_path,
        workspace=tmp_path,
    )
    raw = json.dumps(
        {
            "decision": "emit",
            "name": "Shared blocker chain",
            "observation_type": "dependency",
            "summary": "Three blockers share a two-step dependency chain.",
            "assertions": [{"text": "All blockers are in the supplied chain.", "supporting_claim_ids": [1, 2, 3]}],
        }
    )
    engine = GraphObservationEngine(service.store, llm_call=lambda _system, _prompt: raw)
    engine.repo.queue_discovery(
        tenant_id=None,
        scope="project:test",
        ontology_version="personal-v1",
        cycle_hour="2026-08-12T20",
    )

    discovery = engine.process_discovery(owner="observer", scope="project:test")
    synthesis = engine.process_synthesis(owner="observer", scope="project:test")
    candidates = engine.repo.scope_observations(scope="project:test", tenant_id=None)
    review = review_observation_candidates(service.store, scope="project:test")
    observation_id = int(candidates[0]["observation_claim_id"])
    ordinary = public_recall(
        "blocker dependency",
        scope_allowlist=["project:test"],
        retrieval_mode="legacy",
        db=service.store.db_path,
        workspace=tmp_path,
    )
    enriched = public_recall(
        "blocker dependency",
        scope_allowlist=["project:test"],
        retrieval_mode="legacy",
        include_observations=True,
        observation_limit=2,
        db=service.store.db_path,
        workspace=tmp_path,
    )
    graph_due = capture.due_confirmed_graph_claims(scope="project:test", limit=20)
    dashboard = graph_observations_payload(service, scope="project:test")
    capture.retire_source(sources[0].id, reason="fixture retirement")

    assert discovery.synthesis_queued == 1
    assert synthesis.emitted == 1
    assert review["confirmed"] == 1
    assert ordinary.output == baseline.output
    assert ordinary.observations == ()
    assert observation_id not in {int(row["claim_id"]) for row in ordinary.claims}
    assert enriched.observations[0]["claim_id"] == observation_id
    assert "DERIVED OBSERVATIONS" in enriched.output
    assert observation_id not in {int(row["claim_id"]) for row in graph_due}
    assert dashboard["observations"][0]["observation_type"] == "dependency"
    assert len(dashboard["observations"][0]["supports"]) == 4
    assert dashboard["observations"][0]["lifecycle"]
    assert service.store.get_claim(observation_id).status == "stale"


def test_dashboard_hydration_adds_observation_panel_and_scripts() -> None:
    html = "__GRAPH_OBSERVATIONS_SECTION____GRAPH_OBSERVATIONS_FUNCTIONS____GRAPH_OBSERVATIONS_EVENTS__"

    hydrated = hydrate_dashboard_html(html)

    assert "Derived Observations" in hydrated
    assert "refreshGraphObservations" in hydrated
    assert "graph-observation-refresh" in hydrated


def test_versioned_offline_corpus_meets_structural_quality_gate() -> None:
    corpus = Path("benchmarks/fixtures/graph_observations_v1.json")
    payload = json.loads(corpus.read_text(encoding="utf-8"))
    categories = {str(case["category"]) for case in payload["cases"]}

    report = evaluate_corpus(corpus)

    assert report["cases"] >= 40
    assert {
        "dependency_chain",
        "root_cause",
        "recurring_pattern",
        "unrelated_similarity",
        "hub",
        "conflict",
        "retired_evidence",
        "scope_boundary",
        "sensitive_data",
    } <= categories
    assert report["precision"] >= 0.95
    assert report["recall"] >= 0.95
    assert report["failures"] == []


def test_provider_failure_is_retryable_and_creates_no_observation(tmp_path) -> None:
    service, _capture, _sources = _graph_fixture(tmp_path)

    def provider_failure(_system: str, _prompt: str) -> str:
        raise TimeoutError("fixture provider timeout")

    engine = GraphObservationEngine(service.store, llm_call=provider_failure)
    engine.repo.queue_discovery(
        tenant_id=None,
        scope="project:test",
        ontology_version="personal-v1",
        cycle_hour="2026-08-12T23",
    )
    engine.process_discovery(owner="observer", scope="project:test")

    result = engine.process_synthesis(owner="observer", scope="project:test")
    with service.store.connect() as conn:
        job = conn.execute("SELECT status, error_code FROM graph_observation_jobs WHERE stage='synthesize'").fetchone()

    assert result.failed == 1
    assert engine.repo.scope_observations(scope="project:test", tenant_id=None) == []
    assert tuple(job) == ("retryable", "synthesis_failed")


def test_dream_cycle_enforces_one_three_call_batch_per_scope(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class FakeRepository:
        def queue_discovery(self, **kwargs):
            calls.append(("queue", str(kwargs["tenant_id"])))
            return None, True

    class FakeEngine:
        def __init__(self, _store, *, llm_call):
            self.repo = FakeRepository()

        def process_discovery(self, *, owner, scope):
            calls.append(("discover", scope))
            return SimpleNamespace(synthesis_queued=4, failed=0)

        def process_synthesis(self, *, owner, scope):
            calls.append(("synthesize", scope))
            return SimpleNamespace(emitted=3, failed=0)

    import memorymaster.knowledge.graph_observation_engine as engine_module

    monkeypatch.setattr(engine_module, "GraphObservationEngine", FakeEngine)
    worker = DreamWorker(
        None,
        SimpleNamespace(store=object()),
        object(),
        object(),
        now=lambda: datetime(2026, 8, 12, 23, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        worker,
        "_observation_scope_pairs",
        lambda _scope: [("project:test", "tenant-a"), ("project:test", "tenant-b")],
    )

    result = worker._run_graph_observations(owner="dream-worker", scope="project:test", synthesize=True)

    assert calls.count(("discover", "project:test")) == 1
    assert calls.count(("synthesize", "project:test")) == 1
    assert result["discovery_queued"] == 2
    assert result["emitted"] == 3
