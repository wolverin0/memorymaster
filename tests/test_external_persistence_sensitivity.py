from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from memorymaster.bridges import dream_bridge, qmd_bridge
from memorymaster.core.models import Citation, Claim
from memorymaster.govern import steward
from memorymaster.govern.jobs import compactor
from memorymaster.recall.qdrant_backend import EMBEDDING_DIMS, QdrantBackend

_LITERAL = "sk-ant-api03-NOTAREALKEY000000000000000000000000abcdefghijkl"
_ENCODED = base64.b64encode(_LITERAL.encode()).decode()


def _claim(claim_id: int, **changes) -> Claim:
    base = Claim(
        id=claim_id,
        text=f"safe claim {claim_id}",
        idempotency_key=None,
        normalized_text=None,
        claim_type="fact",
        subject="safe subject",
        predicate="uses",
        object_value="safe object",
        scope="project:test",
        volatility="stable",
        status="confirmed",
        confidence=0.9,
        pinned=False,
        supersedes_claim_id=None,
        replaced_by_claim_id=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        last_validated_at=None,
        archived_at=None,
    )
    return replace(base, **changes)


def _citation(field: str) -> Citation:
    values = {"source": "safe", "locator": "safe", "excerpt": "safe"}
    values[field] = _ENCODED
    return Citation(id=9, claim_id=2, created_at="2026-01-01T00:00:00Z", **values)


@pytest.mark.parametrize("field", ["text", "subject", "predicate", "object_value"])
def test_qmd_skips_encoded_secret_in_every_claim_tuple_field(field: str) -> None:
    unsafe = _claim(2, **{field: _ENCODED})
    safe = _claim(1)
    original = replace(unsafe)

    assert qmd_bridge.claims_to_qmd([unsafe, safe]) == [{"type": "fact", "tier": "working", "text": safe.text}]
    assert unsafe == original


@pytest.mark.parametrize("field", ["source", "locator", "excerpt"])
def test_qmd_skips_encoded_secret_in_every_citation_field(field: str) -> None:
    unsafe = _claim(2, citations=[_citation(field)])
    safe = _claim(1)

    exported = qmd_bridge.claims_to_qmd([unsafe, safe])

    assert [row["text"] for row in exported] == [safe.text]


def test_dream_seed_skips_complete_unsafe_representation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    unsafe = {"id": 2, "text": "looks safe", "subject": _ENCODED, "citations": []}
    safe = {"id": 1, "text": "safe durable dream", "scope": "project:test"}
    monkeypatch.setattr(
        dream_bridge,
        "_open_db",
        lambda _path: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(
        dream_bridge,
        "_query_exportable_claims",
        lambda *args, **kwargs: [unsafe, safe],
    )
    monkeypatch.setattr(dream_bridge, "discover_memory_dir", lambda _path=None: tmp_path)

    result = dream_bridge.dream_seed("unused.db")
    rendered = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.glob("*.md"))

    assert result["seeded"] == 1
    assert result["skipped"] == 1
    assert _ENCODED not in rendered
    assert "safe durable dream" in rendered


def test_exportable_claims_loads_citations_in_one_query_for_multiple_claims() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE claims (
            id INTEGER PRIMARY KEY, status TEXT, tier TEXT,
            quality_score REAL, access_count INTEGER
        );
        CREATE TABLE citations (
            claim_id INTEGER, source TEXT, locator TEXT, excerpt TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO claims VALUES (?, 'confirmed', 'working', 1.0, 0)",
        [(claim_id,) for claim_id in range(1, 5)],
    )
    conn.executemany(
        "INSERT INTO citations VALUES (?, 'source', 'locator', 'excerpt')",
        [(claim_id,) for claim_id in range(1, 5)],
    )
    queries: list[str] = []
    conn.set_trace_callback(queries.append)

    claims = dream_bridge._query_exportable_claims(conn, max_memories=4)

    citation_queries = [query for query in queries if "FROM citations" in query]
    assert len(citation_queries) == 1
    assert [claim["citations"][0]["source"] for claim in claims] == ["source"] * 4


def test_steward_artifact_redacts_without_mutating_input(tmp_path: Path) -> None:
    payload = {"safe": "kept", "decision": {"citation": {"source": _ENCODED}}}
    original = json.loads(json.dumps(payload))
    path = tmp_path / "steward.json"

    steward._write_artifact(path, payload)
    persisted = path.read_text(encoding="utf-8")

    assert _ENCODED not in persisted
    assert json.loads(persisted)["safe"] == "kept"
    assert "sensitivity" in persisted
    assert payload == original


class _CompactorStore:
    def __init__(self, claims: list[Claim]) -> None:
        self.claims = claims
        self.archived: list[int] = []

    def find_for_compaction(self, *, retain_days: int) -> list[Claim]:
        return self.claims

    def list_citations(self, claim_id: int) -> list[Citation]:
        return next(claim.citations for claim in self.claims if claim.id == claim_id)

    def delete_old_events(self, _days: int) -> int:
        return 0

    def transition_claim(self, claim_id: int, **_kwargs) -> None:
        self.archived.append(claim_id)

    def record_event(self, **_kwargs) -> None:
        pass


def test_compactor_skips_unsafe_artifact_and_preserves_safe_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _CompactorStore([_claim(1), _claim(2, citations=[_citation("excerpt")])])
    monkeypatch.setattr(
        compactor,
        "_archive_claims_after_artifacts",
        lambda _store, claims, _days: store.archived.extend(claim.id for claim in claims) or len(claims),
    )

    compactor.run(store, artifacts_dir=tmp_path)
    rendered = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.glob("*.json"))

    assert _ENCODED not in rendered
    assert "safe claim 1" in rendered
    assert '"skipped_sensitive": 1' in rendered
    assert store.archived == [1]


class _FakeClient:
    def __init__(self) -> None:
        self.puts: list[dict] = []

    def put(self, _url: str, *, json: dict) -> SimpleNamespace:
        self.puts.append(json)
        return SimpleNamespace(raise_for_status=lambda: None)


def _unsafe_qdrant_backend() -> QdrantBackend:
    backend = QdrantBackend()
    backend._client.close()
    backend._client = _FakeClient()
    backend._embed = lambda _text: [0.0] * EMBEDDING_DIMS
    return backend


@pytest.mark.parametrize("field", ["text", "subject", "predicate", "object_value"])
def test_qdrant_rejects_unsafe_claim_before_embed_or_upsert(field: str) -> None:
    backend = _unsafe_qdrant_backend()

    assert backend.upsert_claim(_claim(2, **{field: _ENCODED})) is False
    assert backend._client.puts == []


@pytest.mark.parametrize("field", ["source", "locator", "excerpt"])
def test_qdrant_rejects_unsafe_citation_before_embed_or_upsert(field: str) -> None:
    backend = _unsafe_qdrant_backend()

    assert backend.upsert_claim(_claim(2, citations=[_citation(field)])) is False
    assert backend._client.puts == []
