"""Adversarial SQLite semantics for principal-local claim identities."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.stores.storage import SQLiteStore


TENANT = "tenant-principal-identities"
SCOPE = "project:principal-identities"
CITATIONS = [CitationInput(source="identity-red", locator="fixture")]


def _bootstrap(db_path: Path) -> None:
    MemoryService(db_path, workspace_root=db_path.parent).init_db()


def _service(db_path: Path, principal: str) -> MemoryService:
    return MemoryService(
        db_path,
        workspace_root=db_path.parent,
        tenant_id=TENANT,
        require_tenant=True,
        principal=principal,
        allowed_scopes={SCOPE},
    )


def _ingest(
    service: MemoryService,
    *,
    text: str,
    key: str | None,
    visibility: str,
    subject: str = "shared-subject",
    predicate: str = "uses",
):
    return service.ingest(
        text=text,
        citations=CITATIONS,
        idempotency_key=key,
        subject=subject,
        predicate=predicate,
        object_value="shared-value",
        scope=SCOPE,
        visibility=visibility,
    )


def _direct_create(
    store: SQLiteStore,
    *,
    text: str,
    key: str,
    principal: str,
    visibility: str,
):
    return store.create_claim(
        text,
        CITATIONS,
        idempotency_key=key,
        subject="direct-subject",
        predicate="uses",
        object_value="direct-value",
        scope=SCOPE,
        tenant_id=TENANT,
        source_agent=principal,
        visibility=visibility,
    )


def _confirm(service: MemoryService, claim_id: int) -> None:
    transition_claim(
        service.store,
        claim_id,
        "confirmed",
        reason="principal-identity-contract",
        event_type="validator",
    )


def test_service_allows_alice_and_bob_same_private_key_text_and_human_id(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "private-service.db"
    _bootstrap(db_path)
    alice = _service(db_path, "alice")
    bob = _service(db_path, "bob")

    alice_claim = _ingest(
        alice,
        text="The private build uses the same cache.",
        key="private-import-key",
        visibility="private",
    )
    bob_claim = _ingest(
        bob,
        text="The private build uses the same cache.",
        key="private-import-key",
        visibility="private",
    )

    assert alice_claim.id != bob_claim.id
    assert alice_claim.human_id == bob_claim.human_id
    assert alice_claim.source_agent == "alice"
    assert bob_claim.source_agent == "bob"


def test_service_content_hash_is_principal_local_for_private_claims(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "private-content-hash.db"
    _bootstrap(db_path)
    alice_claim = _ingest(
        _service(db_path, "alice"),
        text="Identical private content without a caller key.",
        key=None,
        visibility="private",
    )
    bob_claim = _ingest(
        _service(db_path, "bob"),
        text="Identical private content without a caller key.",
        key=None,
        visibility="private",
    )

    assert alice_claim.id != bob_claim.id
    # Keep the historical content-hash bytes stable.  Namespace partitioning
    # belongs in lookup/index context, not in the digest material.
    assert alice_claim.idempotency_key == bob_claim.idempotency_key


def test_same_principal_private_reingest_still_deduplicates(tmp_path: Path) -> None:
    db_path = tmp_path / "same-principal.db"
    _bootstrap(db_path)
    alice = _service(db_path, "alice")

    first = _ingest(
        alice,
        text="Alice private first payload.",
        key="alice-private-key",
        visibility="private",
    )
    duplicate = _ingest(
        alice,
        text="Alice private changed payload.",
        key="alice-private-key",
        visibility="private",
    )

    assert duplicate.id == first.id


def test_public_identity_remains_tenant_wide_across_principals(tmp_path: Path) -> None:
    db_path = tmp_path / "public-tenant.db"
    _bootstrap(db_path)

    first = _ingest(
        _service(db_path, "alice"),
        text="Tenant-wide public identity.",
        key="public-shared-key",
        visibility="public",
    )
    duplicate = _ingest(
        _service(db_path, "bob"),
        text="Changed public payload must still dedupe.",
        key="public-shared-key",
        visibility="public",
    )

    assert duplicate.id == first.id


def test_public_and_private_identity_namespaces_can_coexist(tmp_path: Path) -> None:
    db_path = tmp_path / "public-private.db"
    _bootstrap(db_path)
    alice = _service(db_path, "alice")

    public = _ingest(
        alice,
        text="Same deterministic human identity.",
        key="cross-visibility-key",
        visibility="public",
    )
    private = _ingest(
        alice,
        text="Same deterministic human identity.",
        key="cross-visibility-key",
        visibility="private",
    )

    assert public.id != private.id
    assert public.human_id == private.human_id


def test_nonpublic_identity_uses_exact_visibility_namespace(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "exact-visibility.db")
    store.init_db()

    private = _direct_create(
        store,
        text="Same non-public identity.",
        key="nonpublic-exact-key",
        principal="alice",
        visibility="private",
    )
    sensitive = _direct_create(
        store,
        text="Same non-public identity.",
        key="nonpublic-exact-key",
        principal="alice",
        visibility="sensitive",
    )

    assert private.id != sensitive.id
    assert private.human_id == sensitive.human_id
    transition_claim(
        store,
        private.id,
        "confirmed",
        reason="private namespace",
        event_type="validator",
    )
    transition_claim(
        store,
        sensitive.id,
        "confirmed",
        reason="sensitive namespace",
        event_type="validator",
    )


def test_visibility_is_normalized_and_validated_before_service_dedup(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "visibility-boundary.db"
    _bootstrap(db_path)
    alice = _service(db_path, "alice")

    normalized = _ingest(
        alice,
        text="Normalize this private visibility.",
        key="normalize-visibility-key",
        visibility=" PRIVATE ",
    )
    duplicate = _ingest(
        alice,
        text="The normalized key must dedupe.",
        key="normalize-visibility-key",
        visibility="private",
    )

    assert normalized.visibility == "private"
    assert duplicate.id == normalized.id
    with pytest.raises(ValueError, match="(?i)visibility"):
        _ingest(
            alice,
            text="Invalid visibility cannot hide behind an existing key.",
            key="normalize-visibility-key",
            visibility="private-or-public",
        )


def test_team_sensitive_write_is_denied_before_any_store_access(tmp_path: Path) -> None:
    db_path = tmp_path / "team-sensitive-denial.db"
    _bootstrap(db_path)
    service = _service(db_path, "alice")

    class NoStoreAccess:
        def __getattr__(self, name: str):
            raise AssertionError(f"store accessed before sensitive denial: {name}")

    service.store = NoStoreAccess()
    with pytest.raises((PermissionError, ValueError), match="(?i)(sensitive|visibility)"):
        _ingest(
            service,
            text="Team runtime cannot persist sensitive visibility.",
            key="team-sensitive-denied",
            visibility="sensitive",
        )


def test_direct_store_does_not_collapse_private_principals(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "private-direct.db")
    store.init_db()

    alice = _direct_create(
        store,
        text="Direct private identity.",
        key="direct-private-key",
        principal="alice",
        visibility="private",
    )
    bob = _direct_create(
        store,
        text="Direct private identity.",
        key="direct-private-key",
        principal="bob",
        visibility="private",
    )

    assert alice.id != bob.id
    assert alice.human_id == bob.human_id


@pytest.mark.parametrize("source_agent", [None, "", "   "])
def test_direct_store_rejects_blank_nonpublic_source_agent(
    tmp_path: Path,
    source_agent: str | None,
) -> None:
    store = SQLiteStore(tmp_path / f"blank-source-{source_agent!r}.db")
    store.init_db()

    with pytest.raises(ValueError, match="(?i)(source|principal|agent)"):
        store.create_claim(
            "Unowned private claim.",
            CITATIONS,
            idempotency_key="unowned-private-key",
            scope=SCOPE,
            tenant_id=TENANT,
            source_agent=source_agent,
            visibility="private",
        )

    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0


def test_foreign_principal_cannot_revive_archived_public_identity(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "archived-public-owner.db"
    _bootstrap(db_path)
    alice = _service(db_path, "alice")
    bob = _service(db_path, "bob")
    archived = _ingest(
        alice,
        text="Alice owns this archived public identity.",
        key="archived-public-key",
        visibility="public",
    )
    transition_claim(
        alice.store,
        archived.id,
        "archived",
        reason="fixture archive",
        event_type="transition",
    )

    try:
        _ingest(
            bob,
            text="Bob must not revive Alice's archived public claim.",
            key="archived-public-key",
            visibility="public",
        )
    except PermissionError:
        pass

    persisted = alice.store.get_claim(archived.id)
    assert persisted is not None
    assert persisted.status == "archived"


def test_confirmed_tuple_namespace_is_public_or_exact_nonpublic_principal(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "confirmed-tuples.db"
    _bootstrap(db_path)
    alice = _service(db_path, "alice")
    bob = _service(db_path, "bob")

    alice_private = _ingest(
        alice,
        text="Alice private tuple.",
        key="tuple-alice-private",
        visibility="private",
    )
    bob_private = _ingest(
        bob,
        text="Bob private tuple.",
        key="tuple-bob-private",
        visibility="private",
    )
    public = _ingest(
        alice,
        text="Public tuple.",
        key="tuple-public-a",
        visibility="public",
    )
    duplicate_public = _ingest(
        bob,
        text="Duplicate public tuple.",
        key="tuple-public-b",
        visibility="public",
    )
    duplicate_alice_private = _ingest(
        alice,
        text="Duplicate Alice private tuple.",
        key="tuple-alice-private-2",
        visibility="private",
    )

    for claim in (alice_private, bob_private, public):
        _confirm(alice if claim.source_agent == "alice" else bob, claim.id)

    with pytest.raises(sqlite3.IntegrityError):
        _confirm(bob, duplicate_public.id)
    with pytest.raises(sqlite3.IntegrityError):
        _confirm(alice, duplicate_alice_private.id)
