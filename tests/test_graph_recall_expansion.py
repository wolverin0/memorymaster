"""Opt-in vector/lexical-first claim-graph expansion (GraphRAG track, 4.9.0).

Covers ``MEMORYMASTER_RECALL_GRAPH_MODE`` (off | vector_first | graph_first):
seeds come from already-authorized recall rows, expansion walks existing
claim/entity edges under fanout/candidate/token caps, and every expanded claim
and every support is rehydrated and re-authorized in SQLite.  Fallback to the
current recall on no seeds / no valid supports / error / timeout.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from memorymaster.capture import CaptureRepository
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.knowledge.entity_registry import resolve_or_create
from memorymaster.recall.claim_edges import ensure_claim_edges_schema
from memorymaster.recall.embeddings import EmbeddingProvider

SCOPE = "project:graph-recall"
AGENT = "graph-test"


@pytest.fixture(autouse=True)
def _hermetic_graph_env(monkeypatch, tmp_path_factory):
    for name in (
        "MEMORYMASTER_RECALL_GRAPH_MODE",
        "MEMORYMASTER_RECALL_GRAPH_EXPAND_HOPS",
        "MEMORYMASTER_RECALL_GRAPH_EXPAND_SEEDS",
        "MEMORYMASTER_RECALL_GRAPH_EXPAND_FANOUT",
        "MEMORYMASTER_RECALL_GRAPH_EXPAND_MAX_CANDIDATES",
        "MEMORYMASTER_RECALL_GRAPH_EXPAND_TOKEN_BUDGET",
        "MEMORYMASTER_RECALL_GRAPH_EXPAND_BASE_SHARE",
        "MEMORYMASTER_RECALL_GRAPH_EXPAND_DEADLINE_MS",
        "MEMORYMASTER_QUERY_CACHE",
        "MEMORYMASTER_LLM_RERANK",
    ):
        monkeypatch.delenv(name, raising=False)
    if os.environ.get("MEMORYMASTER_SKIP_PERF"):
        # Shared CI runners: a 250 ms expansion fell back to "timeout" before its stats were
        # measured (Windows 2026-09-24). Deadline tests pass their own ExpansionCaps.
        monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_EXPAND_DEADLINE_MS", "900")
    monkeypatch.setenv("MEMORYMASTER_EMBEDDING_PROVIDER", "hash")
    monkeypatch.setenv(
        "MEMORYMASTER_DECISIONS_DB",
        str(tmp_path_factory.mktemp("decisions") / "decisions.db"),
    )


class Graph:
    """Small governed corpus with real claim/entity edges on a temp DB."""

    def __init__(self, tmp_path: Path, *, tenant_id: str | None = None) -> None:
        self.tmp_path = tmp_path
        self.db = tmp_path / "graph-recall.db"
        self.svc = self.service(tenant_id=tenant_id)
        self.svc.init_db()
        with sqlite3.connect(self.db) as conn:
            ensure_claim_edges_schema(conn)
        self._external = None

    def service(self, *, tenant_id: str | None = None) -> MemoryService:
        svc = MemoryService(self.db, workspace_root=self.tmp_path, tenant_id=tenant_id)
        svc.embedding_provider = EmbeddingProvider(model="hash-v1", dims=1536)
        return svc

    def claim(
        self,
        text: str,
        *,
        scope: str = SCOPE,
        status: str = "confirmed",
        source_agent: str = AGENT,
        visibility: str = "public",
        claim_type: str | None = None,
        evidence: bool = True,
        svc: MemoryService | None = None,
    ):
        svc = svc or self.svc
        claim = svc.ingest(
            text,
            [CitationInput(source="test://graph", locator=text[:24])],
            scope=scope,
            source_agent=source_agent,
            visibility=visibility,
            claim_type=claim_type,
        )
        if evidence:
            self.attach_evidence(claim.id, f"ev-{claim.id}")
        path = {
            "candidate": [],
            "confirmed": ["confirmed"],
            "stale": ["confirmed", "stale"],
            "archived": ["confirmed", "archived"],
            "superseded": ["confirmed", "superseded"],
        }[status]
        for to_status in path:
            claim = svc.store.apply_status_transition(
                claim, to_status=to_status, reason="fixture", event_type="transition"
            )
        return claim

    def attach_evidence(self, claim_id: int, key: str) -> int:
        if self._external is None:
            self._external = self.svc.upsert_external_source(
                source_type="graph-recall-fixture", display_name="graph recall"
            )
        source = self.svc.upsert_source_item(
            source_id=self._external.id,
            source_item_id=key,
            item_type="text",
            sensitivity="none",
        )
        evidence = self.svc.add_evidence_item(
            source_item_id=source.id, evidence_type="text", sensitivity="none"
        )
        CaptureRepository(self.svc.store).link_claim_evidence(
            claim_id=claim_id, evidence_item_id=evidence.id
        )
        return source.id

    def sql(self, statement: str, params: tuple = ()) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute(statement, params)

    def entity(self, name: str, entity_type: str = "system") -> int:
        with sqlite3.connect(self.db) as conn:
            entity_id = resolve_or_create(conn, name, entity_type=entity_type, scope=SCOPE)
            conn.commit()
        return entity_id

    def mention(self, claim_id: int, *entity_ids: int) -> None:
        for entity_id in entity_ids:
            self.sql(
                "INSERT OR IGNORE INTO claim_entity_links (claim_id, entity_id) VALUES (?, ?)",
                (claim_id, entity_id),
            )

    def relation(
        self, source: int, target: int, relation: str, supporting_claim_id: int
    ) -> None:
        now = "2026-09-23T00:00:00+00:00"
        self.sql(
            """INSERT INTO entity_edge_supports
               (source_entity_id, target_entity_id, relation, supporting_claim_id,
                scope, ontology_version, created_at)
               VALUES (?, ?, ?, ?, ?, 'personal-v1', ?)""",
            (source, target, relation, supporting_claim_id, SCOPE, now),
        )
        self.sql(
            """INSERT OR IGNORE INTO entity_edges
               (source_id, target_id, relation, weight, claim_id, created_at, last_reinforced_at)
               VALUES (?, ?, ?, 1.0, ?, ?, ?)""",
            (source, target, relation, supporting_claim_id, now, now),
        )

    def claim_edge(self, src: int, dst: int, kind: str = "mentions") -> None:
        self.sql(
            "INSERT OR IGNORE INTO claim_edges (src_claim_id, dst_claim_id, edge_kind, created_at) "
            "VALUES (?, ?, ?, '2026-09-23T00:00:00+00:00')",
            (src, dst, kind),
        )

    def claim_link(self, src: int, dst: int, link_type: str = "relates_to") -> None:
        self.svc.add_claim_link(src, dst, link_type)

    def rows(self, query: str, *, svc: MemoryService | None = None, **kwargs):
        kwargs.setdefault("limit", 5)
        kwargs.setdefault("retrieval_mode", "hybrid")
        kwargs.setdefault("include_stale", False)
        kwargs.setdefault("include_conflicted", False)
        kwargs.setdefault("scope_allowlist", [SCOPE])
        kwargs.setdefault("record_accesses", False)
        return (svc or self.svc).query_rows(query, **kwargs)


def _ids(rows) -> list[int]:
    return [row["claim"].id for row in rows]


def _qdrant_world(tmp_path: Path) -> tuple[Graph, dict[str, object]]:
    """seed --mentions Qdrant--(located_in, supported by S)--Atlas NAS<--candidate."""
    g = Graph(tmp_path)
    seed = g.claim("qdrant stores the recall vectors for memorymaster")
    support = g.claim("Qdrant is deployed on the Atlas NAS host")
    target = g.claim("the storage box takes nightly snapshots at three")
    qdrant = g.entity("Qdrant")
    atlas = g.entity("Atlas NAS", "device")
    g.mention(seed.id, qdrant)
    g.mention(support.id, qdrant, atlas)
    g.mention(target.id, atlas)
    g.relation(qdrant, atlas, "located_in", support.id)
    return g, {"seed": seed, "support": support, "target": target,
               "qdrant": qdrant, "atlas": atlas}


# ---------------------------------------------------------------------------
# (1) The real failure of the current enrichment, then the new mode
# ---------------------------------------------------------------------------


def test_lowercase_query_misses_graph_today_and_vector_first_reaches_it(tmp_path, monkeypatch):
    g, w = _qdrant_world(tmp_path)
    target = w["target"].id

    capitalized = g.rows("where does Qdrant keep recall vectors", enrich_with_entities=True)
    lowercase = g.rows("where does qdrant keep recall vectors", enrich_with_entities=True)
    # Current enrichment only seeds from Capitalized words: same question,
    # same graph, different casing -> the supported neighbour disappears.
    assert target in _ids(capitalized)
    assert target not in _ids(lowercase)

    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    expanded = g.rows("where does qdrant keep recall vectors")
    assert _ids(expanded)[0] == w["seed"].id
    assert target in _ids(expanded)
    row = next(row for row in expanded if row["claim"].id == target)
    assert row["source"] == "graph_expansion"
    explanation = row["graph_explanation"]
    assert explanation["seed_claim_id"] == w["seed"].id
    assert w["support"].id in explanation["supporting_claim_ids"]
    assert explanation["citations"], "every supported path carries citations"


def test_vector_first_applies_to_legacy_lexical_recall(tmp_path, monkeypatch):
    g, w = _qdrant_world(tmp_path)
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    rows = g.rows("qdrant recall vectors", retrieval_mode="legacy")
    assert _ids(rows)[0] == w["seed"].id
    assert w["target"].id in _ids(rows)


def test_explicit_mode_argument_overrides_environment(tmp_path, monkeypatch):
    g, w = _qdrant_world(tmp_path)
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    off = g.rows("qdrant recall vectors", graph_mode="off")
    assert w["target"].id not in _ids(off)
    monkeypatch.delenv("MEMORYMASTER_RECALL_GRAPH_MODE")
    on = g.rows("qdrant recall vectors", graph_mode="vector_first")
    assert w["target"].id in _ids(on)


# ---------------------------------------------------------------------------
# (2) Every expanded claim and support is rehydrated and authorized
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["archived", "superseded", "stale"])
def test_retired_or_inactive_candidate_is_never_expanded(tmp_path, monkeypatch, status):
    g = Graph(tmp_path)
    seed = g.claim("pipeline alpha feeds the ranking job")
    retired = g.claim("ranking job output lands in bucket seven", status=status)
    g.claim_link(seed.id, retired.id)
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    rows = g.rows("pipeline alpha", include_stale=True)
    assert retired.id not in _ids(rows)
    assert _ids(rows) == _ids(g.rows("pipeline alpha", include_stale=True, graph_mode="off"))


@pytest.mark.parametrize("status", ["archived", "superseded", "stale"])
def test_retired_support_invalidates_the_path(tmp_path, monkeypatch, status):
    g, w = _qdrant_world(tmp_path)
    # Re-point the only relation support to a retired claim.
    retired = g.claim("Qdrant was on the Atlas NAS until the migration", status=status)
    g.sql("DELETE FROM entity_edge_supports")
    g.relation(w["qdrant"], w["atlas"], "located_in", retired.id)
    g.sql("DELETE FROM claim_entity_links WHERE claim_id = ?", (w["support"].id,))
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    rows = g.rows("qdrant recall vectors", include_stale=True)
    assert w["target"].id not in _ids(rows)
    assert all(row.get("source") != "graph_expansion" for row in rows)
    stats = g.svc.last_graph_expansion["stats"]
    if status in {"archived", "superseded"}:
        assert stats["retired_rejections"] >= 1  # measured, not just filtered
    else:
        assert stats["rejections"].get("support:inactive", 0) >= 1


def test_support_with_retired_source_evidence_is_rejected(tmp_path, monkeypatch):
    g, w = _qdrant_world(tmp_path)
    g.sql("DELETE FROM claim_entity_links WHERE claim_id = ?", (w["support"].id,))
    with sqlite3.connect(g.db) as conn:
        source_item_id = conn.execute(
            """SELECT ei.source_item_id FROM claim_evidence_links cel
               JOIN evidence_items ei ON ei.id = cel.evidence_item_id
               WHERE cel.claim_id = ?""",
            (w["support"].id,),
        ).fetchone()[0]
    CaptureRepository(g.svc.store).retire_source(source_item_id, reason="source withdrawn")
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    rows = g.rows("qdrant recall vectors")
    assert w["target"].id not in _ids(rows)


def test_out_of_scope_candidate_and_support_are_rejected(tmp_path, monkeypatch):
    g = Graph(tmp_path)
    seed = g.claim("pipeline alpha feeds the ranking job")
    foreign = g.claim("ranking job secrets rotate weekly", scope="project:other")
    g.claim_link(seed.id, foreign.id)
    # Support out of scope: entity path whose relation support lives elsewhere.
    alpha = g.entity("Pipeline Alpha")
    beta = g.entity("Bucket Seven", "device")
    target = g.claim("bucket seven keeps thirty days of output")
    other_support = g.claim("Pipeline Alpha writes to Bucket Seven", scope="project:other")
    g.mention(seed.id, alpha)
    g.mention(target.id, beta)
    g.relation(alpha, beta, "uses", other_support.id)
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    rows = g.rows("pipeline alpha")
    assert foreign.id not in _ids(rows)
    assert target.id not in _ids(rows)
    # Without an allowlist the seed's own scope bounds the expansion.
    unscoped = g.rows("pipeline alpha", scope_allowlist=None)
    assert foreign.id not in _ids(unscoped)
    assert target.id not in _ids(unscoped)


def test_other_tenant_candidate_is_rejected(tmp_path, monkeypatch):
    g = Graph(tmp_path, tenant_id="tenant-a")
    other = g.service(tenant_id="tenant-b")
    seed = g.claim("pipeline alpha feeds the ranking job")
    foreign = g.claim("ranking job output lands in bucket seven", svc=other)
    local = g.claim("ranking job runs at dawn")
    # The tenant-bound service refuses cross-tenant links, so the edge is
    # written the way a stale or foreign writer could have left it.
    g.sql(
        "INSERT INTO claim_links (source_id, target_id, link_type, created_at) "
        "VALUES (?, ?, 'relates_to', '2026-09-23T00:00:00+00:00')",
        (seed.id, foreign.id),
    )
    g.claim_link(seed.id, local.id)
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    rows = g.rows("pipeline alpha")
    assert local.id in _ids(rows)
    assert foreign.id not in _ids(rows)


def test_other_principal_private_claim_is_rejected(tmp_path, monkeypatch):
    g = Graph(tmp_path)
    seed = g.claim("pipeline alpha feeds the ranking job")
    private = g.claim(
        "ranking job output lands in bucket seven",
        source_agent="agent-b",
        visibility="private",
    )
    g.claim_link(seed.id, private.id)
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    as_a = g.rows("pipeline alpha", requesting_agent="agent-a")
    assert private.id not in _ids(as_a)
    as_b = g.rows("pipeline alpha", requesting_agent="agent-b")
    assert private.id in _ids(as_b)


def test_private_support_of_other_principal_cannot_carry_a_path(tmp_path, monkeypatch):
    g, w = _qdrant_world(tmp_path)
    private = g.claim(
        "Qdrant is deployed on the Atlas NAS host per agent b notes",
        source_agent="agent-b",
        visibility="private",
    )
    g.sql("DELETE FROM entity_edge_supports")
    g.sql("DELETE FROM claim_entity_links WHERE claim_id = ?", (w["support"].id,))
    g.relation(w["qdrant"], w["atlas"], "located_in", private.id)
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    rows = g.rows("qdrant recall vectors", requesting_agent="agent-a")
    assert w["target"].id not in _ids(rows)


def test_graph_first_with_requesting_agent_anchors_on_that_principal_only(tmp_path, monkeypatch):
    """Another author's public base row must not widen the anchor to its private claims."""
    from memorymaster.recall import graph_expansion

    g, w = _qdrant_world(tmp_path)
    shared = g.claim("the atlas nas is backed up every night", source_agent="agent-b")
    private = g.claim(
        "spare disks sit in the second drawer of rack four",
        source_agent="agent-b",
        visibility="private",
    )
    g.mention(shared.id, w["atlas"])
    g.mention(private.id, w["atlas"])
    query = "is the atlas nas backed up"
    # A second principal (agent-b) is present among agent-a's authorized base rows.
    assert shared.id in _ids(g.rows(query, requesting_agent="agent-a", graph_mode="off"))

    anchors: list[frozenset] = []
    real = graph_expansion._path_authorized

    def spy(authorizer, candidate_id, path):
        anchors.append(path.anchor.principals)
        return real(authorizer, candidate_id, path)

    monkeypatch.setattr(graph_expansion, "_path_authorized", spy)
    rows = g.rows(query, requesting_agent="agent-a", graph_mode="graph_first", limit=10)
    assert w["target"].id in _ids(rows)  # positive control: the arm expanded
    assert private.id not in _ids(rows)
    assert len(_ids(rows)) == len(set(_ids(rows)))  # graph_first's own base-row dedupe guard
    assert anchors and set(anchors) == {frozenset({"agent-a"})}
    # The owner reaches the same claim through the same arm.
    as_owner = g.rows(query, requesting_agent="agent-b", graph_mode="graph_first", limit=10)
    assert private.id in _ids(as_owner)


def test_legacy_enrichment_never_exposes_unauthorized_support_citations(tmp_path):
    """Current enrichment re-authorizes candidates but not their supports."""
    g, w = _qdrant_world(tmp_path)
    private = g.claim(
        "Qdrant is deployed on the Atlas NAS host per agent b notes",
        source_agent="agent-b",
        visibility="private",
    )
    g.sql("DELETE FROM entity_edge_supports")
    g.sql("DELETE FROM claim_entity_links WHERE claim_id = ?", (w["support"].id,))
    g.relation(w["qdrant"], w["atlas"], "located_in", private.id)
    rows = g.rows(
        "where does Qdrant keep recall vectors",
        enrich_with_entities=True,
        requesting_agent="agent-a",
    )
    for row in rows:
        explanation = row.get("graph_explanation") or {}
        assert private.id not in explanation.get("supporting_claim_ids", [])
        assert all(c.get("claim_id") != private.id for c in explanation.get("citations", []))


def test_candidate_or_support_without_citation_is_rejected(tmp_path, monkeypatch):
    g, w = _qdrant_world(tmp_path)
    g.sql("DELETE FROM citations WHERE claim_id = ?", (w["support"].id,))
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    rows = g.rows("qdrant recall vectors")
    assert w["target"].id not in _ids(rows)
    assert all(row.get("source") != "graph_expansion" for row in rows)

    (tmp_path / "second").mkdir()
    g2 = Graph(tmp_path / "second")
    seed = g2.claim("pipeline alpha feeds the ranking job")
    uncited = g2.claim("ranking job output lands in bucket seven")
    g2.sql("UPDATE citations SET source = '  ' WHERE claim_id = ?", (uncited.id,))
    g2.claim_link(seed.id, uncited.id)
    assert uncited.id not in _ids(g2.rows("pipeline alpha"))


def test_sensitive_candidate_is_never_expanded(tmp_path, monkeypatch):
    g = Graph(tmp_path)
    seed = g.claim("pipeline alpha feeds the ranking job")
    sensitive = g.claim("ranking job credentials live in the vault", visibility="sensitive")
    g.claim_link(seed.id, sensitive.id)
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    assert sensitive.id not in _ids(g.rows("pipeline alpha"))


def test_generated_observation_is_never_evidence_for_itself(tmp_path, monkeypatch):
    g, w = _qdrant_world(tmp_path)
    observation = g.claim(
        "the storage appliance must stay reachable at all times",
        claim_type="observation",
        source_agent="memorymaster-graph-observer",
    )
    # The observation is both the only relation support and the candidate.
    g.sql("DELETE FROM entity_edge_supports")
    g.sql("DELETE FROM claim_entity_links WHERE claim_id IN (?, ?)",
          (w["support"].id, w["target"].id))
    g.mention(observation.id, w["atlas"])
    g.relation(w["qdrant"], w["atlas"], "depends_on", observation.id)
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    rows = g.rows("qdrant recall vectors")
    assert observation.id not in _ids(rows)

    # A generated observation cannot support a path to an ordinary claim either.
    g.mention(w["target"].id, w["atlas"])
    rows = g.rows("qdrant recall vectors")
    assert w["target"].id not in _ids(rows)


def test_registered_graph_observation_is_never_a_support(tmp_path, monkeypatch):
    """Generated output is recognised by its registry row, not only its type."""
    g, w = _qdrant_world(tmp_path)
    derived = g.claim("the storage appliance must stay reachable at all times")
    g.sql(
        """INSERT INTO graph_observations
           (observation_claim_id, observation_type, name, scope, tenant_id,
            support_hash, algorithm_version, ontology_version, created_at, updated_at)
           VALUES (?, 'dependency', 'qdrant needs atlas', ?, NULL, ?, 'v1',
                   'personal-v1', '2026-09-23T00:00:00+00:00', '2026-09-23T00:00:00+00:00')""",
        (derived.id, SCOPE, "a" * 64),
    )
    g.sql("DELETE FROM entity_edge_supports")
    g.sql("DELETE FROM claim_entity_links WHERE claim_id = ?", (w["support"].id,))
    g.relation(w["qdrant"], w["atlas"], "depends_on", derived.id)
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    rows = g.rows("qdrant recall vectors")
    assert w["target"].id not in _ids(rows)
    assert g.svc.last_graph_expansion["stats"]["rejections"].get(
        "support:generated_support", 0) >= 1


def _leaf_pair(tmp_path: Path):
    """seed linked to a clean leaf (positive control) and to a leaf under test."""
    g = Graph(tmp_path)
    seed = g.claim("pipeline alpha feeds the ranking job")
    clean = g.claim("ranking job runs at dawn")
    probe = g.claim("ranking job output lands in bucket seven")
    g.claim_link(seed.id, clean.id)
    return g, seed, clean, probe


@pytest.mark.parametrize(
    ("column", "value"),
    [("valid_until", "2020-01-01T00:00:00+00:00"), ("valid_from", "2999-01-01T00:00:00+00:00")],
)
def test_expired_or_future_candidate_is_never_expanded(tmp_path, monkeypatch, column, value):
    g, seed, clean, probe = _leaf_pair(tmp_path)
    g.claim_link(seed.id, probe.id)
    g.sql(f"UPDATE claims SET {column} = ? WHERE id = ?", (value, probe.id))
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    ids = _ids(g.rows("pipeline alpha"))
    assert clean.id in ids
    assert probe.id not in ids
    assert g.svc.last_graph_expansion["stats"]["rejections"].get("candidate:not_current") == 1


def test_public_claim_with_sensitive_text_is_never_expanded(tmp_path, monkeypatch):
    g, seed, clean, probe = _leaf_pair(tmp_path)
    g.claim_link(seed.id, probe.id)
    # Public visibility, but the stored text itself is sensitive.
    g.sql("UPDATE claims SET text = ? WHERE id = ?",
          ("ranking job key [REDACTED:api_key] rotates", probe.id))
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    ids = _ids(g.rows("pipeline alpha"))
    assert clean.id in ids
    assert probe.id not in ids


def test_citation_that_carries_a_secret_is_rejected(tmp_path, monkeypatch):
    g, seed, clean, probe = _leaf_pair(tmp_path)
    g.claim_link(seed.id, probe.id)
    g.sql("UPDATE citations SET locator = ? WHERE claim_id = ?",
          ("password=hunter2hunter2", probe.id))
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    rows = g.rows("pipeline alpha")
    assert clean.id in _ids(rows)
    assert probe.id not in _ids(rows)
    assert "hunter2" not in str([row.get("graph_explanation") for row in rows])


def test_candidate_with_sensitive_evidence_lineage_is_rejected(tmp_path, monkeypatch):
    g, seed, clean, probe = _leaf_pair(tmp_path)
    g.claim_link(seed.id, probe.id)
    g.sql(
        """UPDATE evidence_items SET sensitivity = 'high' WHERE id IN
           (SELECT evidence_item_id FROM claim_evidence_links WHERE claim_id = ?)""",
        (probe.id,),
    )
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    ids = _ids(g.rows("pipeline alpha"))
    assert clean.id in ids
    assert probe.id not in ids
    assert g.svc.last_graph_expansion["stats"]["rejections"].get(
        "candidate:evidence_sensitive") == 1


@pytest.mark.parametrize("link_type", ["supersedes", "contradicts"])
def test_lifecycle_links_are_never_walked(tmp_path, monkeypatch, link_type):
    g, seed, clean, probe = _leaf_pair(tmp_path)
    g.claim_link(seed.id, probe.id, link_type)
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    ids = _ids(g.rows("pipeline alpha"))
    assert clean.id in ids
    assert probe.id not in ids


# ---------------------------------------------------------------------------
# (3) Caps and base quota
# ---------------------------------------------------------------------------


def _star(tmp_path: Path, n: int, *, text_len: int = 0):
    g = Graph(tmp_path)
    seed = g.claim("pipeline alpha feeds the ranking job")
    padding = " filler" * text_len
    leaves = [g.claim(f"leaf {i} holds shard number {i}{padding}") for i in range(n)]
    for leaf in leaves:
        g.claim_link(seed.id, leaf.id)
    return g, seed, leaves


def test_fanout_cap_bounds_neighbours_per_node(tmp_path, monkeypatch):
    g, seed, leaves = _star(tmp_path, 8)
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_EXPAND_FANOUT", "2")
    rows = g.rows("pipeline alpha", limit=10)
    added = [row for row in rows if row.get("source") == "graph_expansion"]
    assert len(added) == 2


def test_candidate_cap_bounds_total_expansion(tmp_path, monkeypatch):
    g, seed, leaves = _star(tmp_path, 8)
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_EXPAND_MAX_CANDIDATES", "3")
    rows = g.rows("pipeline alpha", limit=10)
    added = [row for row in rows if row.get("source") == "graph_expansion"]
    assert len(added) == 3


def test_token_budget_bounds_added_text(tmp_path, monkeypatch):
    g, seed, leaves = _star(tmp_path, 4, text_len=40)  # ~70 tokens each
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_EXPAND_TOKEN_BUDGET", "150")
    rows = g.rows("pipeline alpha", limit=10)
    added = [row for row in rows if row.get("source") == "graph_expansion"]
    assert 1 <= len(added) <= 2
    assert sum(len(row["claim"].text) for row in added) / 4 <= 150


def test_hops_cap_and_two_hop_reach(tmp_path, monkeypatch):
    g = Graph(tmp_path)
    seed = g.claim("pipeline alpha feeds the ranking job")
    hop1 = g.claim("ranking job writes the shard index")
    hop2 = g.claim("shard index is compacted every sunday")
    hop3 = g.claim("sunday compaction pages the on call rota")
    g.claim_link(seed.id, hop1.id)
    g.claim_link(hop1.id, hop2.id, "depends_on")
    g.claim_edge(hop2.id, hop3.id, "mentions")
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    two = _ids(g.rows("pipeline alpha", limit=10))
    assert {hop1.id, hop2.id} <= set(two)
    assert hop3.id not in two  # never beyond two hops
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_EXPAND_HOPS", "1")
    one = _ids(g.rows("pipeline alpha", limit=10))
    assert hop1.id in one and hop2.id not in one
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_EXPAND_HOPS", "9")  # clamped to 2
    assert hop3.id not in _ids(g.rows("pipeline alpha", limit=10))


def test_expansion_never_displaces_all_base_results(tmp_path, monkeypatch):
    g, seed, leaves = _star(tmp_path, 6)
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_EXPAND_BASE_SHARE", "0")  # floor: 1 slot
    rows = g.rows("pipeline alpha", limit=3)
    assert len(rows) == 3
    assert _ids(rows)[0] == seed.id
    one = g.rows("pipeline alpha", limit=1)
    assert _ids(one) == [seed.id]


def test_base_quota_is_kept_ahead_of_expansion(tmp_path, monkeypatch):
    g = Graph(tmp_path)
    base = [g.claim(f"pipeline alpha stage {i} feeds the ranking job") for i in range(4)]
    leaves = [g.claim(f"leaf {i} holds shard number {i}") for i in range(6)]
    for leaf in leaves:
        g.claim_link(base[0].id, leaf.id)
    off = _ids(g.rows("pipeline alpha stage", limit=4, graph_mode="off"))
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    rows = g.rows("pipeline alpha stage", limit=4)
    assert len(rows) == 4
    assert _ids(rows)[:2] == off[:2]  # default base share 0.5 -> 2 of 4 slots


@pytest.mark.parametrize("graph_first", [False, True])
def test_merge_reserves_base_quota_even_when_expansion_outscores_base(graph_first):
    """The quota, not score order, keeps base rows: pin it on the merge itself."""
    from memorymaster.recall.graph_expansion import _merge

    base = [{"id": f"b{i}", "score": 0.1} for i in range(4)]
    extra = [{"id": f"e{i}", "score": 0.9, "source": "graph_expansion"} for i in range(4)]

    def ids(limit, share):
        return [row["id"] for row in _merge(base, extra, limit=limit, base_share=share,
                                            graph_first=graph_first)]

    assert ids(1, 0.0) == ["b0"]  # base share 0 still keeps one base row
    assert ids(4, 0.5)[:2] == ["b0", "b1"]
    assert "e0" in ids(4, 0.5)  # and expansion still gets the remaining room


@pytest.mark.parametrize("retrieval_mode", ["hybrid", "legacy"])
def test_base_row_linked_to_another_base_row_is_never_duplicated(
    tmp_path, monkeypatch, retrieval_mode
):
    """A neighbour recall already returned is not expanded a second time."""
    g = Graph(tmp_path)
    first = g.claim("pipeline alpha feeds the ranking job")
    second = g.claim("pipeline alpha retries the ranking job twice")
    leaf = g.claim("ranking job output lands in bucket seven")
    g.claim_link(first.id, second.id)
    g.claim_link(first.id, leaf.id)
    base = _ids(g.rows("pipeline alpha", limit=10, retrieval_mode=retrieval_mode, graph_mode="off"))
    assert {first.id, second.id} <= set(base) and leaf.id not in base

    rows = g.rows("pipeline alpha", limit=10, retrieval_mode=retrieval_mode,
                  graph_mode="vector_first")
    ids = _ids(rows)
    assert len(ids) == len(set(ids)), ids
    assert [row["claim"].id for row in rows if row.get("source") == "graph_expansion"] == [leaf.id]


# ---------------------------------------------------------------------------
# (4) Fallback paths and non-regression
# ---------------------------------------------------------------------------


def test_no_seeds_falls_back_to_current_recall(tmp_path, monkeypatch):
    g = Graph(tmp_path)
    seed = g.claim("pipeline alpha feeds the ranking job", status="stale")
    leaf = g.claim("ranking job output lands in bucket seven")
    g.claim_link(seed.id, leaf.id)
    baseline = _ids(g.rows("pipeline alpha", include_stale=True, graph_mode="off"))
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    assert _ids(g.rows("pipeline alpha", include_stale=True)) == baseline
    assert g.svc.last_graph_expansion["fallback_reason"] == "no_seeds"
    assert _ids(g.rows("nothing matches zzzz")) == []


def test_no_valid_supports_falls_back_to_current_enrichment(tmp_path, monkeypatch):
    g, w = _qdrant_world(tmp_path)
    for claim_id in (w["support"].id, w["target"].id):
        g.sql("DELETE FROM citations WHERE claim_id = ?", (claim_id,))
    current = _ids(
        g.rows("where does Qdrant keep recall vectors", enrich_with_entities=True,
               graph_mode="off")
    )
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    rows = g.rows("where does Qdrant keep recall vectors", enrich_with_entities=True)
    assert _ids(rows) == current
    assert g.svc.last_graph_expansion["fallback_reason"] == "no_valid_supports"


def test_error_falls_back_to_current_recall(tmp_path, monkeypatch):
    from memorymaster.recall import graph_expansion

    g, w = _qdrant_world(tmp_path)
    baseline = _ids(g.rows("qdrant recall vectors", graph_mode="off"))

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(graph_expansion, "_neighbour_steps", _boom)
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    assert _ids(g.rows("qdrant recall vectors")) == baseline
    report = g.svc.last_graph_expansion
    assert report["fallback_reason"] == "error"
    assert "malformed" not in str(report)  # no raw exception text in reports


def test_timeout_discards_late_expansion(tmp_path, monkeypatch):
    from memorymaster.recall import graph_expansion

    g, w = _qdrant_world(tmp_path)
    baseline = _ids(g.rows("qdrant recall vectors", graph_mode="off"))
    ticks = iter(range(0, 10_000, 1))  # every clock read advances 1 s
    monkeypatch.setattr(graph_expansion, "_clock", lambda: float(next(ticks)))
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    assert _ids(g.rows("qdrant recall vectors")) == baseline
    assert g.svc.last_graph_expansion["fallback_reason"] == "timeout"


def test_answer_that_finishes_after_the_deadline_never_acts(tmp_path, monkeypatch):
    from memorymaster.recall import graph_expansion

    g, w = _qdrant_world(tmp_path)
    baseline = _ids(g.rows("qdrant recall vectors", graph_mode="off"))
    clock = {"now": 0.0}
    monkeypatch.setattr(graph_expansion, "_clock", lambda: clock["now"])
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    assert w["target"].id in _ids(g.rows("qdrant recall vectors"))  # positive control

    real_merge = graph_expansion._merge

    def merge_that_overruns(*args, **kwargs):
        rows = real_merge(*args, **kwargs)
        clock["now"] += 10.0  # the result is ready only after the deadline
        return rows

    monkeypatch.setattr(graph_expansion, "_merge", merge_that_overruns)
    rows = g.rows("qdrant recall vectors")
    assert _ids(rows) == baseline
    assert all(row.get("source") != "graph_expansion" for row in rows)
    assert g.svc.last_graph_expansion["fallback_reason"] == "timeout"


def test_failed_expansion_falls_back_to_current_enrichment_not_bare_recall(tmp_path, monkeypatch):
    from memorymaster.recall import graph_expansion

    g, w = _qdrant_world(tmp_path)
    query = "where does Qdrant keep recall vectors"
    current = _ids(g.rows(query, enrich_with_entities=True, graph_mode="off"))
    bare = _ids(g.rows(query, graph_mode="off"))
    assert w["target"].id in current and w["target"].id not in bare  # the sides differ

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(graph_expansion, "_neighbour_steps", _boom)
    rows = g.rows(query, enrich_with_entities=True, graph_mode="vector_first")
    assert g.svc.last_graph_expansion["fallback_reason"] == "error"
    assert _ids(rows) == current


def test_graph_rows_bypass_the_query_cache_in_both_directions(tmp_path, monkeypatch):
    g, w = _qdrant_world(tmp_path)
    monkeypatch.setenv("MEMORYMASTER_QUERY_CACHE", "1")
    off_first = _ids(g.rows("qdrant recall vectors", graph_mode="off"))
    assert w["target"].id not in off_first
    # A cached off-mode answer is never served to an expansion call ...
    on = g.rows("qdrant recall vectors", graph_mode="vector_first")
    assert w["target"].id in _ids(on)
    # ... and expanded rows are never cached for an off-mode call.
    off_again = g.rows("qdrant recall vectors", graph_mode="off")
    assert _ids(off_again) == off_first
    assert all(row.get("source") != "graph_expansion" for row in off_again)


def _expand_direct(g: Graph, base_rows, *, get_claim=None, caps=None, mode="vector_first",
                   limit=10):
    from memorymaster.recall import graph_expansion

    return graph_expansion.expand_recall(
        base_rows,
        "pipeline alpha",
        mode=mode,
        limit=limit,
        db_path=str(g.db),
        get_claim=get_claim or (lambda cid: g.svc.store.get_claim(cid, include_citations=True)),
        annotate=lambda _claim: None,
        statuses=["confirmed"],
        scope_allowlist=[SCOPE],
        tenant_id=None,
        requesting_agent=None,
        caps=caps,
    )


def test_deadline_is_checked_per_step_not_only_per_node(tmp_path, monkeypatch):
    """A slow rehydration cannot keep a whole fanout running past the deadline."""
    from memorymaster.recall import graph_expansion

    g, seed, leaves = _star(tmp_path, 8)
    base = g.rows("pipeline alpha", graph_mode="off")
    clock = {"now": 0.0, "calls": 0}

    def slow_get_claim(claim_id):
        clock["calls"] += 1
        clock["now"] += 0.1  # every rehydration costs 100 ms
        return g.svc.store.get_claim(claim_id, include_citations=True)

    monkeypatch.setattr(graph_expansion, "_clock", lambda: clock["now"])
    caps = graph_expansion.ExpansionCaps(fanout=8, deadline_ms=250)
    outcome = _expand_direct(g, base, get_claim=slow_get_claim, caps=caps)
    assert outcome.applied is False
    assert outcome.fallback_reason == "timeout"
    # seed + at most the steps started before 250 ms, not all 8 neighbours.
    assert clock["calls"] <= 4


def test_sqlite_busy_wait_is_bounded_by_the_deadline(tmp_path, monkeypatch):
    from memorymaster.recall import graph_expansion

    g, seed, leaves = _star(tmp_path, 2)
    seen: dict[str, int] = {}
    real = graph_expansion.connect_ro

    def spy(path, **kwargs):
        seen.update(kwargs)
        return real(path, **kwargs)

    monkeypatch.setattr(graph_expansion, "connect_ro", spy)
    caps = graph_expansion.ExpansionCaps(deadline_ms=120)
    outcome = _expand_direct(g, g.rows("pipeline alpha", graph_mode="off"), caps=caps)
    assert outcome.applied is True
    assert seen.get("query_ms") == 120  # never the 2000 ms reader default


def test_missing_graph_tables_fall_back(tmp_path, monkeypatch):
    g = Graph(tmp_path)
    seed = g.claim("pipeline alpha feeds the ranking job")
    g.sql("DROP TABLE claim_edges")
    baseline = _ids(g.rows("pipeline alpha", graph_mode="off"))
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
    assert _ids(g.rows("pipeline alpha")) == baseline == [seed.id]
    assert g.svc.last_graph_expansion["fallback_reason"] in {"no_candidates", "no_graph"}


def test_ordinary_recall_is_unchanged_without_edges(tmp_path, monkeypatch):
    g = Graph(tmp_path)
    for i in range(6):
        g.claim(f"deploy checklist item {i} covers the release gate")
    for mode in ("hybrid", "legacy"):
        baseline = [
            (row["claim"].id, row["score"])
            for row in g.rows("deploy checklist release", retrieval_mode=mode)
        ]
        monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "vector_first")
        expanded = [
            (row["claim"].id, row["score"])
            for row in g.rows("deploy checklist release", retrieval_mode=mode)
        ]
        monkeypatch.delenv("MEMORYMASTER_RECALL_GRAPH_MODE")
        # Freshness is time-dependent, so scores are compared approximately.
        assert [cid for cid, _ in expanded] == [cid for cid, _ in baseline]
        assert [s for _, s in expanded] == pytest.approx([s for _, s in baseline], rel=1e-6)
        assert g.svc.last_graph_expansion["applied"] is False


def test_default_and_unknown_mode_are_off(tmp_path, monkeypatch):
    from memorymaster.recall import graph_expansion

    assert graph_expansion.resolve_graph_mode(None, environ={}) == "off"
    assert graph_expansion.resolve_graph_mode(None, environ={
        "MEMORYMASTER_RECALL_GRAPH_MODE": "yes-please"}) == "off"
    assert graph_expansion.resolve_graph_mode(None, environ={
        "MEMORYMASTER_RECALL_GRAPH_MODE": " Vector_First "}) == "vector_first"
    assert graph_expansion.resolve_graph_mode("graph_first", environ={}) == "graph_first"
    caps = graph_expansion.ExpansionCaps.from_env({
        "MEMORYMASTER_RECALL_GRAPH_EXPAND_HOPS": "inf",
        "MEMORYMASTER_RECALL_GRAPH_EXPAND_BASE_SHARE": "nan",
        "MEMORYMASTER_RECALL_GRAPH_EXPAND_FANOUT": "many",
        "MEMORYMASTER_RECALL_GRAPH_EXPAND_DEADLINE_MS": "99999",
    })
    assert (caps.max_hops, caps.base_share, caps.fanout, caps.deadline_ms) == (2, 0.5, 6, 900)

    g, w = _qdrant_world(tmp_path)
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MODE", "bogus")
    assert w["target"].id not in _ids(g.rows("qdrant recall vectors"))


def test_graph_first_experimental_arm_seeds_from_lowercase_entities(tmp_path):
    g, w = _qdrant_world(tmp_path)
    rows = g.rows("is the atlas nas backed up", graph_mode="graph_first")
    assert w["target"].id in _ids(rows)
    assert all(
        row["graph_explanation"]["mode"] == "graph_first"
        for row in rows if row.get("source") == "graph_expansion"
    )


def test_postgres_like_store_fails_closed_to_current_recall(tmp_path):
    from memorymaster.recall import graph_expansion

    outcome = graph_expansion.expand_recall(
        [{"claim": object(), "score": 1.0}],
        "anything",
        mode="vector_first",
        limit=5,
        db_path=None,
        get_claim=lambda _cid: None,
        annotate=lambda _claim: None,
        statuses=["confirmed"],
        scope_allowlist=None,
        tenant_id=None,
        requesting_agent=None,
    )
    assert outcome.applied is False
    assert outcome.fallback_reason == "unsupported_store"
