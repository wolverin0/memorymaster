"""T-1865 / F11: dedup must never pair claims across an authorization identity.

Identity = (tenant_id, scope, visibility, private owner). Identical text in two
identities is not a duplicate: archiving one in favour of the other would hide a
tenant's / project's / owner's claim behind someone else's.
"""
from __future__ import annotations

import pytest

from memorymaster.core.models import CitationInput, Claim
from memorymaster.core.service import MemoryService
from memorymaster.govern.jobs import dedup as dedup_job
from memorymaster.govern.jobs.dedup import find_conflicts, find_duplicates
from memorymaster.recall.embeddings import EmbeddingProvider

TEXT = "The deploy pipeline publishes the release artifacts to the staging bucket"


@pytest.fixture(autouse=True)
def _hash_provider(monkeypatch):
    def factory():
        return EmbeddingProvider(model="hash-v1", dims=768)

    monkeypatch.setattr("memorymaster.core.service.create_best_provider", factory)
    monkeypatch.setattr("memorymaster.govern.jobs.dedup.create_best_provider", factory)


def _claim(
    id: int,
    *,
    scope: str = "project:alpha",
    tenant_id: str | None = None,
    visibility: str = "public",
    source_agent: str | None = None,
    confidence: float = 0.5,
    subject: str | None = None,
    predicate: str | None = None,
    object_value: str | None = None,
) -> Claim:
    return Claim(
        id=id, text=TEXT, idempotency_key=None, normalized_text=None, claim_type=None,
        subject=subject, predicate=predicate, object_value=object_value, scope=scope,
        volatility="medium", status="confirmed", confidence=confidence, pinned=False,
        supersedes_claim_id=None, replaced_by_claim_id=None,
        created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00",
        last_validated_at=None, archived_at=None, tenant_id=tenant_id,
        visibility=visibility, source_agent=source_agent, citations=[],
    )


PROVIDER = EmbeddingProvider(model="hash-v1", dims=768)

DIFFERENT_IDENTITY = [
    pytest.param(dict(scope="project:alpha"), dict(scope="project:beta"), id="scope"),
    pytest.param(dict(tenant_id="t1"), dict(tenant_id="t2"), id="tenant"),
    pytest.param(dict(tenant_id="t1"), dict(tenant_id=None), id="tenant-vs-none"),
    pytest.param(
        dict(visibility="private", source_agent="alice"), dict(visibility="public"), id="visibility"
    ),
    pytest.param(
        dict(visibility="private", source_agent="alice"),
        dict(visibility="private", source_agent="bob"),
        id="private-owner",
    ),
]


@pytest.mark.parametrize(("left", "right"), DIFFERENT_IDENTITY)
def test_find_duplicates_never_pairs_across_identity(left, right):
    pairs = find_duplicates([_claim(1, **left), _claim(2, confidence=0.9, **right)], PROVIDER)
    assert pairs == []


@pytest.mark.parametrize(("left", "right"), DIFFERENT_IDENTITY)
def test_find_conflicts_never_pairs_across_identity(left, right):
    a = _claim(1, subject="s", predicate="p", object_value="x", **left)
    b = _claim(2, subject="s", predicate="p", object_value="y", **right)
    assert find_conflicts([a, b], PROVIDER, threshold=0.0) == []


def test_find_conflicts_still_pairs_within_one_identity():
    a = _claim(1, subject="s", predicate="p", object_value="x")
    b = _claim(2, subject="s", predicate="p", object_value="y")
    assert len(find_conflicts([a, b], PROVIDER, threshold=0.0)) == 1


def test_same_identity_still_pairs():
    pairs = find_duplicates(
        [_claim(1, visibility="private", source_agent="alice"),
         _claim(2, visibility="private", source_agent="alice", confidence=0.9)],
        PROVIDER,
    )
    assert [(p.keep_id, p.archive_id) for p in pairs] == [(2, 1)]


def test_public_claims_with_different_authors_are_one_identity():
    # The principal only partitions non-public claims (claim_identity.identity_namespace_key).
    pairs = find_duplicates(
        [_claim(1, source_agent="alice"), _claim(2, source_agent="bob", confidence=0.9)], PROVIDER
    )
    assert len(pairs) == 1


def _ingest(svc: MemoryService, key: str, **kwargs) -> Claim:
    # Straight to the store: the facade's own content dedup (F14) would otherwise
    # collapse the identical rows this test needs to exist.
    defaults = {"idempotency_key": key, "tenant_id": svc.tenant_id}
    defaults.update(kwargs)
    return svc.store.create_claim(TEXT, [CitationInput(source="test")], **defaults)


def _statuses(db_path, ids):
    svc = MemoryService(str(db_path))
    return {i: svc.store.get_claim(i, include_citations=False).status for i in ids}


def test_run_leaves_scope_tenant_and_private_duplicates_alone(tmp_path):
    db = tmp_path / "dedup-auth.db"
    MemoryService(str(db)).init_db()
    alpha = _ingest(MemoryService(str(db)), "k-alpha", scope="project:alpha", confidence=0.9)
    beta = _ingest(MemoryService(str(db)), "k-beta", scope="project:beta", confidence=0.8)
    t1 = _ingest(MemoryService(str(db), tenant_id="t1"), "k-t1", scope="project:gamma", confidence=0.9)
    t2 = _ingest(MemoryService(str(db), tenant_id="t2"), "k-t2", scope="project:gamma", confidence=0.8)
    priv_a = _ingest(
        MemoryService(str(db)), "k-pa", scope="project:delta",
        visibility="private", source_agent="alice", confidence=0.9,
    )
    priv_b = _ingest(
        MemoryService(str(db)), "k-pb", scope="project:delta",
        visibility="private", source_agent="bob", confidence=0.8,
    )
    pub = _ingest(MemoryService(str(db)), "k-pub", scope="project:delta", confidence=0.7)

    result = MemoryService(str(db)).dedup(dry_run=False)

    assert result["duplicates_found"] == 0
    assert result["claims_archived"] == 0
    ids = [alpha.id, beta.id, t1.id, t2.id, priv_a.id, priv_b.id, pub.id]
    assert set(_statuses(db, ids).values()) == {"candidate"}


def test_run_still_archives_same_identity_duplicates(tmp_path):
    db = tmp_path / "dedup-same.db"
    svc = MemoryService(str(db))
    svc.init_db()
    a = _ingest(svc, "k1", visibility="private", source_agent="alice", confidence=0.9)
    b = _ingest(svc, "k2", visibility="private", source_agent="alice", confidence=0.8)
    result = svc.dedup(dry_run=False)
    assert result["claims_archived"] == 1
    assert _statuses(db, [a.id, b.id]) == {a.id: "candidate", b.id: "archived"}


def test_run_rechecks_identity_right_before_archive(tmp_path, monkeypatch):
    """A pair found while the claims shared an identity must not be applied if a
    claim's identity changed before the mutation (stale candidate list)."""
    db = tmp_path / "dedup-recheck.db"
    svc = MemoryService(str(db))
    svc.init_db()
    a = _ingest(svc, "k1", scope="project:alpha", confidence=0.9)
    b = _ingest(svc, "k2", scope="project:alpha", confidence=0.8)

    real_find = dedup_job.find_duplicates

    def find_then_move(*args, **kwargs):
        pairs = real_find(*args, **kwargs)
        assert len(pairs) == 1  # same identity at collection time
        with svc.store.connect() as conn:  # concurrent writer re-scopes the loser
            conn.execute("UPDATE claims SET scope = 'project:beta' WHERE id = ?", (b.id,))
            conn.commit()
        return pairs

    monkeypatch.setattr(dedup_job, "find_duplicates", find_then_move)
    result = svc.dedup(dry_run=False)

    assert result["claims_archived"] == 0
    assert _statuses(db, [a.id, b.id]) == {a.id: "candidate", b.id: "candidate"}
