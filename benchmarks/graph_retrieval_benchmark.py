"""Deterministic governed graph-retrieval benchmark.

Builds a temporary SQLite corpus whose answers are reachable only through
authorized entity-edge supports. The benchmark measures graph top-five hit
rate while treating cross-scope, stale, and sensitive results as hard failures.
No external provider, live database, or persistent source content is used.
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memorymaster.core.models import CitationInput  # noqa: E402
from memorymaster.core.service import MemoryService  # noqa: E402
from memorymaster.knowledge.entity_graph import EntityGraph  # noqa: E402
from memorymaster.recall.embeddings import EmbeddingProvider  # noqa: E402


@dataclass(frozen=True, slots=True)
class GraphCase:
    seed: str
    hub: str
    query: str
    answer: str


CASES = (
    GraphCase("Ariadne", "Project Lumen", "Ariadne schedule", "Every Tuesday at 09:00."),
    GraphCase("Borealis", "Project Cedar", "Borealis cadence", "Reviews happen every second Friday."),
    GraphCase("Cygnus", "Project Harbor", "Cygnus milestone", "The next milestone is the blue launch."),
    GraphCase("Daphne", "Project Quartz", "Daphne ritual", "The team ritual starts after lunch."),
    GraphCase("Erebus", "Project Willow", "Erebus checkpoint", "The checkpoint owner rotates monthly."),
    GraphCase("Fjord", "Project Amber", "Fjord window", "The maintenance window opens at dawn."),
)


def _confirm(
    service: MemoryService,
    text: str,
    scope: str,
    confidence: float = 0.5,
    source_agent: str = "graph-benchmark",
):
    claim = service.ingest(
        text,
        [CitationInput(source="graph-benchmark", locator=scope)],
        scope=scope,
        confidence=confidence,
        source_agent=source_agent,
    )
    return service.store.apply_status_transition(
        claim,
        to_status="confirmed",
        reason="graph benchmark fixture",
        event_type="validator",
    )


def _extract(graph: EntityGraph, claim_id: int, text: str, payload: dict) -> None:
    with patch(
        "memorymaster.knowledge.entity_graph._llm_chat",
        return_value=json.dumps(payload),
    ):
        names = graph.extract_and_link(claim_id, text)
    if not names:
        raise RuntimeError(f"graph fixture extraction failed for claim {claim_id}")


def _entity(name: str, entity_type: str) -> dict:
    return {"name": name, "type": entity_type, "aliases": []}


def _link_claim(graph: EntityGraph, claim, entity_name: str) -> None:
    _extract(
        graph,
        claim.id,
        claim.text,
        {"entities": [_entity(entity_name, "project")], "relations": []},
    )


def _link_edge(graph: EntityGraph, claim, seed: str, hub: str) -> None:
    _extract(
        graph,
        claim.id,
        claim.text,
        {
            "entities": [_entity(seed, "person"), _entity(hub, "project")],
            "relations": [{"source": seed, "target": hub, "relation": "manages"}],
        },
    )


def _seed_case(service: MemoryService, graph: EntityGraph, case: GraphCase, index: int) -> dict:
    scope = f"project:graph-{index}"
    denied_scope = f"project:graph-denied-{index}"
    support = _confirm(service, f"{case.seed} manages {case.hub}.", scope)
    _link_edge(graph, support, case.seed, case.hub)
    target = _confirm(service, case.answer, scope, confidence=0.2)
    _link_claim(graph, target, case.hub)

    stale = _confirm(service, f"Inactive detail {index}.", scope, confidence=0.99)
    _link_claim(graph, stale, case.hub)
    service.store.apply_status_transition(
        stale,
        to_status="stale",
        reason="graph benchmark inactive fixture",
        event_type="validator",
    )

    sensitive = _confirm(service, f"Sensitive detail {index}.", scope, confidence=0.99)
    _link_claim(graph, sensitive, case.hub)
    with service.store.connect() as conn:
        conn.execute("UPDATE claims SET visibility='sensitive' WHERE id=?", (sensitive.id,))
        conn.commit()

    denied_hub = f"Denied {case.hub}"
    denied_support = _confirm(
        service, f"{case.seed} manages {denied_hub}.", denied_scope
    )
    _link_edge(graph, denied_support, case.seed, denied_hub)
    denied_target = _confirm(service, f"Denied detail {index}.", denied_scope)
    _link_claim(graph, denied_target, denied_hub)

    query_topic = case.query.rsplit(maxsplit=1)[-1].lower()
    for distractor in range(7):
        _confirm(
            service,
            f"Generic {case.seed} {query_topic} reference {index}-{distractor}.",
            scope,
            confidence=0.99,
            source_agent=f"graph-distractor-{index}-{distractor}",
        )
    return {
        "scope": scope,
        "target_id": target.id,
        "forbidden_ids": {stale.id, sensitive.id, denied_support.id, denied_target.id},
    }


def run_benchmark(case_limit: int | None = None) -> dict:
    selected = CASES[:case_limit] if case_limit else CASES
    started = time.perf_counter()
    details: list[dict] = []
    with tempfile.TemporaryDirectory(
        prefix="mm-graph-benchmark-", ignore_cleanup_errors=True
    ) as temp_dir:
        root = Path(temp_dir)
        service = MemoryService(root / "graph.db", workspace_root=root)
        service.embedding_provider = EmbeddingProvider(model="hash-v1", dims=64)
        service.init_db()
        graph = EntityGraph(str(root / "graph.db"))
        fixtures = [
            _seed_case(service, graph, case, index)
            for index, case in enumerate(selected, start=1)
        ]
        for case, fixture in zip(selected, fixtures, strict=True):
            rows = service.query_rows(
                case.query,
                limit=5,
                include_stale=False,
                include_conflicted=False,
                include_candidates=False,
                retrieval_mode="hybrid",
                scope_allowlist=[fixture["scope"]],
                enrich_with_entities=True,
                record_accesses=False,
            )
            ids = [row["claim"].id for row in rows]
            forbidden = sorted(set(ids) & fixture["forbidden_ids"])
            details.append(
                {
                    "query": case.query,
                    "scope": fixture["scope"],
                    "target_id": fixture["target_id"],
                    "top5_ids": ids,
                    "top5": [
                        {
                            "id": row["claim"].id,
                            "scope": row["claim"].scope,
                            "status": row["claim"].status,
                            "visibility": row["claim"].visibility,
                            "source": row.get("source", "ranker"),
                        }
                        for row in rows
                    ],
                    "hit": fixture["target_id"] in ids,
                    "forbidden_ids": forbidden,
                }
            )
        del graph, service
        gc.collect()

    hits = sum(bool(item["hit"]) for item in details)
    forbidden_hits = sum(len(item["forbidden_ids"]) for item in details)
    return {
        "graph_hit_rate_at_5": hits / len(details) if details else 0.0,
        "hits": hits,
        "questions": len(details),
        "forbidden_hits": forbidden_hits,
        "provider_calls": 0,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "details": details,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    payload = run_benchmark(args.limit)
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "details"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
