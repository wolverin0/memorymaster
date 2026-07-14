"""Adversarial consumers of principal-local ``human_id`` namespaces.

The identity indexes intentionally permit the same human-readable ID in the
tenant-wide public namespace and in exact non-public principal namespaces.
Every downstream consumer therefore has to preserve or explicitly choose a
namespace; an unordered ``LIMIT 1`` is never a valid resolution policy.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from memorymaster.core.models import Claim, ClaimLink
from memorymaster.knowledge.vault_exporter import export_vault
from memorymaster.recall.claim_edges import MENTION_KIND, extract_edges_for_claim
from memorymaster.surfaces.cli_handlers_basic import _handle_pin
from memorymaster.surfaces.cli_helpers import _resolve_claim_id


TENANT = "tenant-human-id-consumers"
SCOPE = "project:human-id-consumers"
DUPLICATE_HUMAN_ID = "mm-abcd"


def _claim(
    claim_id: int,
    text: str,
    *,
    human_id: str = DUPLICATE_HUMAN_ID,
    visibility: str = "public",
    source_agent: str | None = None,
) -> Claim:
    return Claim(
        id=claim_id,
        text=text,
        idempotency_key=f"key-{claim_id}",
        normalized_text=text.lower(),
        claim_type="fact",
        subject="human-id-consumer",
        predicate="documents",
        object_value=str(claim_id),
        scope=SCOPE,
        volatility="stable",
        status="confirmed",
        confidence=0.9,
        pinned=False,
        supersedes_claim_id=None,
        replaced_by_claim_id=None,
        created_at="2026-07-11T00:00:00+00:00",
        updated_at="2026-07-11T00:00:00+00:00",
        last_validated_at=None,
        archived_at=None,
        human_id=human_id,
        tenant_id=TENANT,
        source_agent=source_agent,
        visibility=visibility,
    )


def _edge_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE claims (
            id INTEGER PRIMARY KEY,
            text TEXT NOT NULL,
            human_id TEXT,
            replaced_by_claim_id INTEGER,
            tenant_id TEXT,
            visibility TEXT NOT NULL,
            source_agent TEXT
        );
        """
    )
    return conn


def _insert_edge_claim(
    conn: sqlite3.Connection,
    claim_id: int,
    *,
    text: str,
    human_id: str,
    visibility: str,
    source_agent: str | None,
    tenant_id: str = TENANT,
) -> None:
    conn.execute(
        """
        INSERT INTO claims
            (id, text, human_id, replaced_by_claim_id, tenant_id, visibility, source_agent)
        VALUES (?, ?, ?, NULL, ?, ?, ?)
        """,
        (claim_id, text, human_id, tenant_id, visibility, source_agent),
    )


def test_claim_edges_public_reference_selects_public_namespace() -> None:
    conn = _edge_connection()
    try:
        # The lower row id is deliberately private so unordered LIMIT 1 picks
        # the forbidden target on SQLite today.
        _insert_edge_claim(
            conn,
            10,
            text="Alice private target",
            human_id=DUPLICATE_HUMAN_ID,
            visibility="private",
            source_agent="alice",
        )
        _insert_edge_claim(
            conn,
            20,
            text="Tenant public target",
            human_id=DUPLICATE_HUMAN_ID,
            visibility="public",
            source_agent="writer",
        )
        _insert_edge_claim(
            conn,
            30,
            text=f"See {DUPLICATE_HUMAN_ID}.",
            human_id="mm-3333",
            visibility="public",
            source_agent="reader",
        )

        edges = extract_edges_for_claim(conn, 30, f"See {DUPLICATE_HUMAN_ID}.")

        assert edges == [(30, 20, MENTION_KIND)]
    finally:
        conn.close()


def test_claim_edges_private_reference_stays_in_source_principal_namespace() -> None:
    conn = _edge_connection()
    try:
        _insert_edge_claim(
            conn,
            10,
            text="Bob private target",
            human_id=DUPLICATE_HUMAN_ID,
            visibility="private",
            source_agent="bob",
        )
        _insert_edge_claim(
            conn,
            20,
            text="Alice private target",
            human_id=DUPLICATE_HUMAN_ID,
            visibility="private",
            source_agent="alice",
        )
        _insert_edge_claim(
            conn,
            30,
            text="Tenant public target",
            human_id=DUPLICATE_HUMAN_ID,
            visibility="public",
            source_agent="writer",
        )
        _insert_edge_claim(
            conn,
            40,
            text=f"Alice cites {DUPLICATE_HUMAN_ID}.",
            human_id="mm-4444",
            visibility="private",
            source_agent="alice",
        )

        edges = extract_edges_for_claim(
            conn,
            40,
            f"Alice cites {DUPLICATE_HUMAN_ID}.",
        )

        assert edges == [(40, 20, MENTION_KIND)]
    finally:
        conn.close()


def test_claim_edges_refuses_cross_principal_private_ambiguity() -> None:
    conn = _edge_connection()
    try:
        for claim_id, principal in ((10, "alice"), (20, "bob")):
            _insert_edge_claim(
                conn,
                claim_id,
                text=f"{principal} private target",
                human_id=DUPLICATE_HUMAN_ID,
                visibility="private",
                source_agent=principal,
            )
        _insert_edge_claim(
            conn,
            30,
            text=f"Public source cites {DUPLICATE_HUMAN_ID}.",
            human_id="mm-3333",
            visibility="public",
            source_agent="reader",
        )

        edges = extract_edges_for_claim(
            conn,
            30,
            f"Public source cites {DUPLICATE_HUMAN_ID}.",
        )

        assert edges == []
    finally:
        conn.close()


class _VaultStore:
    def __init__(self, claims: list[Claim], links: list[ClaimLink] | None = None):
        self._claims = claims
        self._links = links or []

    def list_claims(self, **_kwargs) -> list[Claim]:
        return list(self._claims)

    def get_claim_links(self, claim_id: int) -> list[ClaimLink]:
        return [
            link
            for link in self._links
            if claim_id in (link.source_id, link.target_id)
        ]


def _claim_docs(output: Path) -> list[Path]:
    return sorted(
        path
        for path in output.glob("**/*.md")
        if path.name != "index.md"
    )


def _doc_for_claim(paths: list[Path], claim_id: int) -> Path:
    marker = f"claim_id: {claim_id}\n"
    matches = [
        path
        for path in paths
        if marker in path.read_text(encoding="utf-8")
    ]
    assert len(matches) == 1, (
        f"expected one exported document for claim {claim_id}, found {len(matches)}"
    )
    return matches[0]


def test_vault_export_preserves_duplicate_human_id_documents(tmp_path: Path) -> None:
    claims = [
        _claim(1, "Tenant public document", visibility="public", source_agent="writer"),
        _claim(2, "Alice private document", visibility="private", source_agent="alice"),
    ]

    stats = export_vault(_VaultStore(claims), tmp_path)
    documents = _claim_docs(tmp_path)

    assert stats["exported"] == 2
    assert len(documents) == 2
    assert _doc_for_claim(documents, 1) != _doc_for_claim(documents, 2)


def test_vault_wikilink_targets_exact_duplicate_identity_document(
    tmp_path: Path,
) -> None:
    public = _claim(1, "Tenant public target", visibility="public", source_agent="writer")
    private = _claim(2, "Alice private target", visibility="private", source_agent="alice")
    source = _claim(
        3,
        "Alice private source",
        human_id="mm-3333",
        visibility="private",
        source_agent="alice",
    )
    link = ClaimLink(
        id=1,
        source_id=source.id,
        target_id=private.id,
        link_type="depends_on",
        created_at="2026-07-11T00:00:00+00:00",
    )

    # Put the public collision second: the legacy exporter overwrites Alice's
    # private target while leaving a plausible but wrong ``[[mm-abcd]]`` link.
    export_vault(_VaultStore([private, public, source], [link]), tmp_path)
    documents = _claim_docs(tmp_path)
    target_path = _doc_for_claim(documents, private.id)
    source_path = _doc_for_claim(documents, source.id)
    link_targets = re.findall(
        r"\[\[([^\]|#]+)",
        source_path.read_text(encoding="utf-8"),
    )

    assert any(Path(target).name == target_path.stem for target in link_targets)


class _PolicyAwareStore:
    """Expose a bad legacy default so the CLI must state its safe policy."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def resolve_claim_id(self, _identifier: str, **kwargs) -> int:
        self.calls.append(kwargs)
        return 20 if kwargs.get("visibility") == "public" else 10


class _PinService:
    tenant_id = TENANT

    def __init__(self) -> None:
        self.store = _PolicyAwareStore()
        self.pinned_ids: list[int] = []

    def pin(self, claim_id: int, *, pin: bool):
        self.pinned_ids.append(claim_id)
        return SimpleNamespace(id=claim_id, status="confirmed", pinned=pin)


def test_generic_cli_resolution_declares_public_namespace() -> None:
    service = _PinService()

    resolved = _resolve_claim_id(service, DUPLICATE_HUMAN_ID)  # type: ignore[arg-type]

    assert resolved == 20
    assert service.store.calls == [
        {"tenant_id": TENANT, "visibility": "public", "scope": None}
    ]


def test_mutating_cli_never_pins_arbitrarily_selected_private_claim(
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = _PinService()
    args = argparse.Namespace(claim_id=DUPLICATE_HUMAN_ID, unpin=False)

    _handle_pin(args, service, argparse.ArgumentParser(), "unused.db")

    assert service.pinned_ids == [20]
    assert "claim_id=20" in capsys.readouterr().out
