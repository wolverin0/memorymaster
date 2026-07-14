from __future__ import annotations

import hashlib
from pathlib import Path

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.privacy_ops import PrivacySelector, build_privacy_plan
from memorymaster.recall.verbatim_store import ensure_verbatim_schema, store_verbatim


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_privacy_plan_is_inventory_driven_and_strictly_read_only(tmp_path: Path) -> None:
    db = tmp_path / "privacy.db"
    service = MemoryService(db, workspace_root=tmp_path)
    service.init_db()
    service.ingest(
        "A principal-bound privacy export claim.",
        citations=[CitationInput(source="test", locator="privacy")],
        source_agent="agent-a",
        holder="agent-a",
        scope="project:privacy",
        visibility="private",
    )
    ensure_verbatim_schema(str(db))
    store_verbatim(
        str(db),
        "session-a",
        "user",
        "This is a sufficiently long principal-bound transcript row.",
        scope="project:privacy",
        source_agent="agent-a",
    )
    artifact = tmp_path / "artifacts" / "privacy" / "opaque.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"principal":"agent-a"}', encoding="utf-8")
    backup = tmp_path / ".memorymaster" / "snapshots" / "old.db"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"not-a-live-database")
    before = {path: _sha(path) for path in (db, artifact, backup)}

    plan = build_privacy_plan(
        db_target=db,
        workspace=tmp_path,
        selector=PrivacySelector(principal="agent-a", scope="project:privacy"),
    )

    after = {path: _sha(path) for path in (db, artifact, backup)}
    assert before == after
    assert plan["dry_run"] is True
    assert plan["complete"] is False
    by_surface = {row["surface"]: row for row in plan["surfaces"]}
    assert by_surface["primary_db"]["status"] == "FOUND"
    assert by_surface["verbatim"]["status"] == "FOUND"
    assert by_surface["artifacts"]["status"] == "BLOCKED-EXTERNAL"
    assert by_surface["qdrant"]["status"] == "BLOCKED-EXTERNAL"
    assert by_surface["backups"]["disposition"] == "expire_by_policy"


def test_privacy_selector_requires_a_principal() -> None:
    try:
        PrivacySelector(principal=" ")
    except ValueError as exc:
        assert "principal" in str(exc)
    else:
        raise AssertionError("blank privacy principal was accepted")
