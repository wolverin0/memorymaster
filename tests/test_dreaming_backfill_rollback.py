"""Disposable ownership receipts and cross-path rollback retirement guards."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from memorymaster.core.models import CitationInput, Claim
from memorymaster.core.intake_policy import reset_intake_state
from memorymaster.core.service import MemoryService


@pytest.fixture
def service(tmp_path, monkeypatch):
    reset_intake_state()
    monkeypatch.setattr(MemoryService, "_init_qdrant", staticmethod(lambda: None))
    monkeypatch.setattr("memorymaster.core.webhook.fire_webhook", lambda *args, **kwargs: None)
    monkeypatch.setattr("memorymaster.knowledge.entity_ingest.resolve_claim_entities", lambda *args, **kwargs: 0)
    instance = MemoryService(tmp_path / "rollback.db", workspace_root=tmp_path)
    instance.init_db()
    return instance


def _ingest(service, *, receipt=None, source="original", key="stable-operation"):
    return service.ingest(
        "The project preserves independently supported architecture evidence.",
        [CitationInput(source, locator=f"{source}:1")], source_agent="dream-worker",
        scope="project:test", idempotency_key=key, outcome_receipt=receipt,
        intake_batch_id="cohort-fixture", intake_batch_max=200,
    )


def test_creation_receipt_recovers_owned_operation_after_crash_replay(service, tmp_path):
    first = {"operation_id": "application-1"}
    claim = _ingest(service, receipt=first)
    assert isinstance(claim, Claim)
    assert first["created"] and first["owned"] and first["status"] == "created"
    restarted = MemoryService(service.store.db_path, workspace_root=tmp_path)
    replay = {"operation_id": "application-1"}
    assert _ingest(restarted, receipt=replay, source="different").id == claim.id
    assert replay["status"] == "reused" and not replay["created"] and replay["owned"]
    assert replay["intake_batch_id"] == "cohort-fixture"
    other = {"operation_id": "application-2"}
    _ingest(restarted, receipt=other)
    assert not other["created"] and not other["owned"]
    assert restarted.store.get_claim(claim.id).citations == claim.citations


def test_receipt_never_owns_preexisting_claim_without_creation_operation(service):
    claim = _ingest(service)
    receipt = {"operation_id": "application-1", "created": True, "owned": True}
    assert _ingest(service, receipt=receipt).id == claim.id
    assert receipt["status"] == "reused"
    assert not receipt["created"] and not receipt["owned"]
    assert receipt["creation_operation_id"] is None


@pytest.mark.parametrize("with_receipt", [False, True])
def test_live_dedup_race_skips_post_ingest_writes_on_existing_winner(service, monkeypatch, with_receipt):
    claim = _ingest(service)
    monkeypatch.setattr(service.store, "get_claim_by_idempotency_key", lambda *args, **kwargs: None)
    monkeypatch.setattr(service.store, "_check_idempotency", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "memorymaster.govern.ingest_governance.apply_post_ingest_governance",
        lambda *args, **kwargs: pytest.fail("race winner treated as new intake"),
    )
    receipt = {"operation_id": "later-backfill"} if with_receipt else None
    reused = _ingest(service, receipt=receipt, source="replacement")
    assert reused.id == claim.id and reused.citations == claim.citations
    assert len(service.store.list_events(claim.id)) == 1
    if receipt is not None:
        assert receipt["status"] == "reused" and not receipt["created"] and not receipt["owned"]


def test_concurrent_dedup_receipts_name_only_the_atomic_creation_owner(service, tmp_path, monkeypatch):
    other = MemoryService(service.store.db_path, workspace_root=tmp_path)
    barrier = Barrier(2)
    for store in (service.store, other.store):
        original = store._check_idempotency

        def synchronized_check(*args, _original=original, **kwargs):
            result = _original(*args, **kwargs)
            barrier.wait(timeout=20)
            return result

        monkeypatch.setattr(store, "_check_idempotency", synchronized_check)
    receipts = [{"operation_id": "application-a"}, {"operation_id": "application-b"}]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_ingest, instance, receipt=receipt, source=source)
                   for instance, receipt, source in zip((service, other), receipts, ("alpha", "beta"))]
        claims = [future.result(timeout=30) for future in futures]
    assert claims[0].id == claims[1].id
    assert sum(receipt["created"] for receipt in receipts) == 1
    assert sum(receipt["owned"] for receipt in receipts) == 1
    winner = next(index for index, receipt in enumerate(receipts) if receipt["created"])
    assert service.store.get_claim(claims[0].id).citations[0].source == ("alpha", "beta")[winner]
    assert {receipt["creation_operation_id"] for receipt in receipts} == {receipts[winner]["operation_id"]}


@pytest.mark.parametrize("reason", ["friendly_forget_claim", "ao7_rollback"])
@pytest.mark.parametrize("key", [None, "explicit-key"])
@pytest.mark.parametrize("with_receipt", [False, True])
def test_retired_dedup_race_cannot_revive_or_replace_provenance(service, monkeypatch, reason, key, with_receipt):
    claim = _ingest(service, key=key)
    service.store.record_event(claim_id=claim.id, event_type="audit", details="original_source_review", payload={"source": "original"})
    retired = service.store.apply_status_transition(claim, to_status="archived", reason=reason, event_type="transition")
    # Force both facade lookups and the store's optimistic lookup to miss;
    # the unique constraint must recover the actual concurrent winner.
    monkeypatch.setattr(service.store, "get_claim_by_idempotency_key", lambda *args, **kwargs: None)
    monkeypatch.setattr(service.store, "_check_idempotency", lambda *args, **kwargs: None)
    receipt = {"operation_id": "later-backfill"} if with_receipt else None
    recovered = _ingest(service, receipt=receipt, source="replacement", key=key)
    assert (recovered.id, recovered.status, recovered.version) == (retired.id, "archived", retired.version)
    assert recovered.citations == claim.citations
    assert [event.details for event in service.store.list_events(claim.id)] == [reason, "original_source_review", "claim_ingested"]
    if receipt is not None:
        assert receipt["status"] == "blocked" and not receipt["created"] and not receipt["owned"]


@pytest.mark.parametrize("reason", ["friendly_forget_claim", "ao7_rollback"])
def test_store_retirement_guard_survives_service_check_race(service, monkeypatch, reason):
    claim = _ingest(service)
    stale_archived = service.store.apply_status_transition(
        claim, to_status="archived", reason="ordinary_cleanup", event_type="transition",
    )
    original = service.store.latest_archive_reason

    def retire_after_check(claim_id):
        result = original(claim_id)
        service.store.apply_status_transition(
            stale_archived, to_status="archived", reason=reason, event_type="transition",
        )
        return result

    monkeypatch.setattr(service.store, "latest_archive_reason", retire_after_check)
    recovered = _ingest(service)
    assert recovered.status == service.store.get_claim(claim.id).status == "archived"
    assert not any(event.details.startswith("reingest_revival:") for event in service.store.list_events(claim.id))


def test_runner_rollback_retires_only_owned_candidates_and_blocks_live_replay(service, tmp_path):
    from memorymaster.dreaming.backfill import rollback_backfill_cohort
    from test_dreaming_backfill_cli import _campaign, _claims

    (tmp_path / "campaign").mkdir()
    fixture = _campaign(tmp_path / "campaign")
    fixture.runner.inventory()
    fixture.runner.run(stage="all", apply_candidates=True)
    claim = _claims(fixture)[0]
    cohort = fixture.runner._rows()[0]["cohort_id"]
    result = rollback_backfill_cohort(fixture.runner, cohort, service=fixture.service, dry_run=False)
    assert result["retired_claim_ids"] == [claim.id]
    assert fixture.service.store.get_claim(claim.id).status == "archived"
    replay = fixture.service.ingest(claim.text, [CitationInput("dream-worker", locator="live:replay")],
                                    scope=claim.scope, source_agent="dream-worker",
                                    claim_type=claim.claim_type, subject=claim.subject, predicate=claim.predicate,
                                    object_value=claim.object_value,
                                    intake_batch_id="replay", intake_batch_max=200)
    assert replay.id == claim.id and replay.status == "archived"
    assert not fixture.service.query_rows("cited architecture", include_candidates=True,
                                           scope_allowlist=[claim.scope], record_accesses=False)
    fixture.runner.run(stage="apply", apply_candidates=True)
    assert fixture.service.store.get_claim(claim.id).status == "archived"
    assert fixture.runner._rows()[0]["metadata"]["rollback"]["tombstone"] is True


def test_runner_rollback_preserves_reused_claim_and_its_original_review(service, tmp_path):
    import hashlib
    from memorymaster.dreaming.backfill import rollback_backfill_cohort
    from test_dreaming_backfill_cli import _campaign

    (tmp_path / "campaign").mkdir()
    fixture = _campaign(tmp_path / "campaign")
    fixture.runner.inventory()
    fixture.runner.run(stage="extract")
    row = fixture.runner._rows()[0]
    capture = fixture.runner._captures([row])[0]
    candidate = capture["extraction"][0]
    key = "dream-" + hashlib.sha256(f"{row['scope']}|{candidate['candidate_id']}".encode()).hexdigest()[:24]
    existing = fixture.service.ingest(candidate["text"], [CitationInput("independent", locator="original:source")],
                                      idempotency_key=key, scope=row["scope"], source_agent="dream-worker",
                                      claim_type=candidate["claim_type"], subject=candidate["subject"],
                                      predicate=candidate["predicate"], object_value=candidate["object_value"],
                                      intake_batch_id="original", intake_batch_max=200)
    events = fixture.service.list_events(claim_id=existing.id)
    fixture.runner.run(stage="consolidate")
    fixture.runner.run(stage="quality")
    fixture.runner.run(stage="apply", apply_candidates=True)
    manifest = next(iter(fixture.runner._rows()[0]["metadata"]["applications"].values()))
    assert manifest["reused_claim_ids"] == [existing.id] and manifest["created_claim_ids"] == []
    assert fixture.service.store.get_claim(existing.id).citations == existing.citations
    assert fixture.service.list_events(claim_id=existing.id) == events
    result = rollback_backfill_cohort(fixture.runner, row["cohort_id"], service=fixture.service, dry_run=False)
    assert result["retired_claim_ids"] == [] and result["reused_claim_ids"] == [existing.id]
    assert fixture.service.store.get_claim(existing.id).status == "candidate"


@pytest.mark.parametrize("independent_support", [True, False])
def test_rollback_recovers_crash_intent_and_preserves_independent_support(service, tmp_path, independent_support):
    from memorymaster.dreaming.backfill import rollback_backfill_cohort
    from test_dreaming_backfill_cli import _campaign, _claims

    (tmp_path / "campaign").mkdir()
    fixture = _campaign(tmp_path / "campaign")
    fixture.runner.inventory()
    fixture.runner.run(stage="all", apply_candidates=True)
    claim = _claims(fixture)[0]
    row = fixture.runner._rows()[0]
    entry = next(iter(row["metadata"]["applications"].values()))
    entry["created_claim_ids"], entry["completed"] = [], False
    fixture.runner._save(row)
    if independent_support:
        fixture.service.store.record_event(claim_id=claim.id, event_type="validator", details="independent_verification")
    result = rollback_backfill_cohort(fixture.runner, row["cohort_id"], service=fixture.service, dry_run=False)
    assert result["created_claim_ids"] == [claim.id]
    assert result["retired_claim_ids"] == ([] if independent_support else [claim.id])
    assert fixture.service.store.get_claim(claim.id).status == ("candidate" if independent_support else "archived")


@pytest.mark.parametrize("concurrent_change", ["validator", "version", "citation"])
def test_rollback_preserves_support_added_after_ownership_check(service, tmp_path, monkeypatch, concurrent_change):
    from memorymaster.dreaming.backfill import rollback_backfill_cohort
    from test_dreaming_backfill_cli import _campaign, _claims

    fixture = _campaign(tmp_path)
    fixture.runner.inventory()
    fixture.runner.run(stage="all", apply_candidates=True)
    claim = _claims(fixture)[0]
    cohort = fixture.runner._rows()[0]["cohort_id"]
    original = fixture.service.store.apply_status_transition

    def change_before_transition(current, **kwargs):
        if concurrent_change == "validator":
            fixture.service.store.record_event(claim_id=current.id, event_type="validator", details="independent")
        elif concurrent_change == "citation":
            with fixture.service.store.connect() as conn:
                conn.execute("INSERT INTO citations (claim_id, source, locator, created_at) VALUES (?, ?, ?, ?)",
                             (current.id, "independent", "independent:1", fixture.runner.now().isoformat()))
                conn.commit()
        else:
            original(current, to_status="conflicted", reason="independent_review", event_type="transition")
        return original(current, **kwargs)

    monkeypatch.setattr(fixture.service.store, "apply_status_transition", change_before_transition)
    result = rollback_backfill_cohort(fixture.runner, cohort, service=fixture.service, dry_run=False)
    assert result["retired_claim_ids"] == []
    assert result["preserved_claim_ids"] == [claim.id]
    assert fixture.service.store.get_claim(claim.id).status == ("conflicted" if concurrent_change == "version" else "candidate")


def test_rollback_requires_runner_lease_before_retiring_cohort(service, tmp_path):
    from memorymaster.dreaming.backfill import rollback_backfill_cohort
    from test_dreaming_backfill_cli import _campaign, _claims

    fixture = _campaign(tmp_path)
    fixture.runner.inventory()
    fixture.runner.run(stage="all", apply_candidates=True)
    row = fixture.runner._rows()[0]
    assert fixture.runner.ledger.acquire_lease("backfill-runner", "other-runner", 3600, now=fixture.runner.now())
    with pytest.raises(RuntimeError, match="lease is busy"):
        rollback_backfill_cohort(fixture.runner, row["cohort_id"], service=fixture.service, dry_run=False)
    assert len(_claims(fixture)) == 1
    assert fixture.runner._rows()[0]["status"] == row["status"]


def test_postgres_rejects_conditional_rollback_before_connection():
    from memorymaster.stores.postgres_store import PostgresStore

    store = object.__new__(PostgresStore)
    with pytest.raises(ValueError, match="SQLite-only"):
        store.apply_status_transition(None, to_status="archived", reason="ao7_rollback",
                                      event_type="transition", event_payload={"ao7_retirement_guard": {}})
