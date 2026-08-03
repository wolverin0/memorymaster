from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster import forget, improve, recall, remember
from memorymaster.capture import CaptureRepository
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.public.v1 import API_VERSION


_STAMP = "2026-08-03T00:00:00+00:00"


def test_recall_export_remains_callable_after_subpackage_import() -> None:
    import memorymaster
    import memorymaster.recall

    assert callable(memorymaster.recall)


@pytest.fixture
def public_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    db = tmp_path / "public.db"
    workspace = tmp_path / "demo-workspace"
    workspace.mkdir()
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_ROOTS", f"demo={workspace}")
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_TRUST_MODE", "local-trusted")
    return db, workspace


def _service(db: Path, workspace: Path) -> MemoryService:
    service = MemoryService(str(db), workspace_root=workspace)
    service.init_db()
    return service


def test_lazy_public_exports_and_replay_safe_remember(public_env) -> None:
    db, workspace = public_env
    first = remember(text="The project uses SQLite WAL.", db=db, workspace=workspace)
    second = remember(text="The project uses SQLite WAL.", db=db, workspace=workspace)
    assert first.api_version == API_VERSION
    assert first.evidence is not None
    assert len(first.job_ids) == 1
    assert first.deduplicated is False
    assert second.deduplicated is True
    service = _service(db, workspace)
    assert len(service.list_evidence_items(limit=10)) == 1
    assert CaptureRepository(service.store).status_counts()["pending"] == 1


def test_url_only_capture_preserves_reference_without_fetching(public_env) -> None:
    db, workspace = public_env
    receipt = remember(
        source_uri="https://example.com/reference",
        db=db,
        workspace=workspace,
    )
    assert receipt.evidence is None
    assert receipt.warnings == ("awaiting_evidence",)
    service = _service(db, workspace)
    with service.store.connect() as conn:
        job = conn.execute("SELECT status, error_code FROM capture_jobs").fetchone()
    assert tuple(job) == ("blocked", "awaiting_evidence")


def test_file_capture_persists_only_root_relative_locator(public_env) -> None:
    db, workspace = public_env
    document = workspace / "note.md"
    document.write_text("A durable note.", encoding="utf-8")
    receipt = remember(path=document, db=db, workspace=workspace)
    service = _service(db, workspace)
    item = service.get_source_item_by_id(receipt.source_item["id"])
    assert item is not None
    assert str(workspace) not in (item.payload_json or "")
    assert "demo/note.md" in (item.payload_json or "")


def test_recall_defaults_to_confirmed_only_with_citations(public_env) -> None:
    db, workspace = public_env
    service = _service(db, workspace)
    candidate = service.ingest(
        text="Candidate fact about a nebula.",
        citations=[CitationInput(source="fixture", locator="candidate")],
        scope="project:demo-workspace",
    )
    confirmed = service.ingest(
        text="Confirmed fact about a nebula.",
        citations=[CitationInput(source="fixture", locator="confirmed")],
        scope="project:demo-workspace",
    )
    service.store.apply_status_transition(
        confirmed,
        to_status="confirmed",
        reason="fixture",
        event_type="validator",
    )
    receipt = recall("nebula", db=db, workspace=workspace)
    ids = {item["claim_id"] for item in receipt.claims}
    assert confirmed.id in ids
    assert candidate.id not in ids
    assert receipt.claims[0]["citations"]
    assert "score_explanation" in receipt.claims[0]


def _linked_claim(
    service: MemoryService,
    source_item_id: int,
    evidence_id: int,
    *,
    status: str,
):
    claim = service.ingest(
        text=f"Linked {status} claim",
        citations=[CitationInput(source="fixture", locator=f"evidence:{evidence_id}")],
        scope="project:demo-workspace",
    )
    CaptureRepository(service.store).link_claim_evidence(
        claim_id=claim.id, evidence_item_id=evidence_id
    )
    if status != "candidate":
        claim = service.store.apply_status_transition(
            claim, to_status=status, reason="fixture", event_type="validator"
        )
    return claim


def test_forget_source_preview_then_archives_candidate(public_env) -> None:
    db, workspace = public_env
    captured = remember(text="Candidate source.", db=db, workspace=workspace)
    service = _service(db, workspace)
    claim = _linked_claim(
        service,
        captured.source_item["id"],
        captured.evidence["id"],
        status="candidate",
    )
    preview = forget(
        source_item_id=captured.source_item["id"],
        db=db,
        workspace=workspace,
    )
    assert preview.apply is False
    assert service.store.get_claim(claim.id).status == "candidate"
    forget(
        source_item_id=captured.source_item["id"],
        apply=True,
        db=db,
        workspace=workspace,
    )
    assert service.store.get_claim(claim.id).status == "archived"
    assert service.list_evidence_items(source_item_id=captured.source_item["id"], limit=10)


def test_forget_sole_source_confirmed_becomes_stale(public_env) -> None:
    db, workspace = public_env
    captured = remember(text="Confirmed source.", db=db, workspace=workspace)
    service = _service(db, workspace)
    claim = _linked_claim(
        service,
        captured.source_item["id"],
        captured.evidence["id"],
        status="confirmed",
    )
    forget(
        source_item_id=captured.source_item["id"],
        apply=True,
        db=db,
        workspace=workspace,
    )
    assert service.store.get_claim(claim.id).status == "stale"
    assert claim.id not in {
        item["claim_id"]
        for item in recall("Confirmed source", db=db, workspace=workspace).claims
    }


def test_forget_claim_is_previewed_and_archive_only(public_env) -> None:
    db, workspace = public_env
    service = _service(db, workspace)
    claim = service.ingest(
        text="Forget this direct claim.",
        citations=[],
        scope="project:demo-workspace",
    )
    preview = forget(claim_id=claim.id, db=db, workspace=workspace)
    assert preview.actions[0]["to_status"] == "archived"
    forget(claim_id=claim.id, apply=True, db=db, workspace=workspace)
    assert service.store.get_claim(claim.id).status == "archived"
    forget(claim_id=claim.id, apply=True, db=db, workspace=workspace)


def test_improve_queues_without_promoting_candidates(public_env) -> None:
    db, workspace = public_env
    service = _service(db, workspace)
    source = service.upsert_external_source(
        source_type="fixture", display_name="Fixture"
    )
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id="fixture-item",
        item_type="text",
        text="Evidence awaiting extraction.",
        payload_json={"scope": "project:demo-workspace"},
        content_hash="a" * 64,
    )
    service.add_evidence_item(
        source_item_id=item.id,
        evidence_type="text",
        text="Evidence awaiting extraction.",
        content_hash="a" * 64,
    )
    candidate = service.ingest(
        text="Candidate remains governed.",
        citations=[],
        scope="project:demo-workspace",
    )
    receipt = improve(db=db, workspace=workspace)
    assert receipt.queued["extract_claims"] == 1
    assert receipt.steward_review_due == 1
    assert service.store.get_claim(candidate.id).status == "candidate"


def test_improve_finds_requested_scope_beyond_global_scan_window(public_env) -> None:
    db, workspace = public_env
    service = _service(db, workspace)
    source = service.upsert_external_source(
        source_type="fixture", display_name="Enumeration fixture"
    )
    rows = [
        (
            source.id,
            f"other-{index}",
            "text",
            f"Other scope evidence {index}",
            '{"scope":"project:other"}',
            "a" * 64,
            _STAMP,
            _STAMP,
        )
        for index in range(801)
    ]
    rows.append(
        (
            source.id,
            "wanted",
            "text",
            "Requested scope evidence",
            '{"scope":"project:demo-workspace"}',
            "b" * 64,
            _STAMP,
            _STAMP,
        )
    )
    with service.store.connect() as conn:
        conn.executemany(
            """INSERT INTO source_items
               (source_id, source_item_id, item_type, text, payload_json,
                content_hash, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        source_rows = conn.execute(
            "SELECT id, source_item_id, text, content_hash FROM source_items ORDER BY id"
        ).fetchall()
        conn.executemany(
            """INSERT INTO evidence_items
               (source_item_id, evidence_type, text, content_hash, created_at)
               VALUES (?, 'text', ?, ?, ?)""",
            [
                (row["id"], row["text"], row["content_hash"], _STAMP)
                for row in source_rows
            ],
        )

    receipt = improve(db=db, workspace=workspace, max_items=200)

    assert receipt.queued["extract_claims"] == 1
    with service.store.connect() as conn:
        wanted_job = conn.execute(
            """SELECT j.status FROM capture_jobs j
               JOIN source_items s ON s.id=j.source_item_id
               WHERE s.source_item_id='wanted' AND j.stage='extract_claims'"""
        ).fetchone()
    assert wanted_job is not None


def test_repeated_improve_advances_beyond_existing_graph_job_prefix(public_env) -> None:
    db, workspace = public_env
    service = _service(db, workspace)
    source = service.upsert_external_source(
        source_type="fixture", display_name="Graph enumeration fixture"
    )
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id="graph-evidence",
        item_type="text",
        text="Graph evidence",
        payload_json={"scope": "project:demo-workspace"},
        content_hash="c" * 64,
    )
    evidence = service.add_evidence_item(
        source_item_id=item.id,
        evidence_type="text",
        text="Graph evidence",
        content_hash="c" * 64,
    )
    with service.store.connect() as conn:
        conn.executemany(
            """INSERT INTO claims
               (text, scope, status, confidence, created_at, updated_at)
               VALUES (?, 'project:demo-workspace', 'confirmed', 1.0, ?, ?)""",
            [(f"Confirmed graph claim {index}", _STAMP, _STAMP) for index in range(201)],
        )
        claim_ids = [
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM claims WHERE status='confirmed' ORDER BY id"
            ).fetchall()
        ]
        conn.executemany(
            """INSERT INTO claim_evidence_links
               (claim_id, evidence_item_id, role, created_at)
               VALUES (?, ?, 'support', ?)""",
            [(claim_id, evidence.id, _STAMP) for claim_id in claim_ids],
        )

    first = improve(db=db, workspace=workspace, max_items=200)
    second = improve(db=db, workspace=workspace, max_items=200)

    assert first.queued["extract_graph"] == 200
    assert second.queued["extract_graph"] == 1
    with service.store.connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM capture_jobs WHERE stage='extract_graph'"
        ).fetchone()[0]
    assert total == 201
