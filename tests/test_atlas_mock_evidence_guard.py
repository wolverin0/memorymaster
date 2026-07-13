"""Red contract: normal Atlas media commands must never mint mock evidence."""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core.service import MemoryService
from memorymaster.surfaces.cli import main


def _run_cli_allowing_required_provider(argv: list[str]) -> int:
    """Normalize argparse's future provider-required failure into an exit code."""
    try:
        return main(argv)
    except SystemExit as exc:
        return int(exc.code or 0)


@pytest.mark.parametrize(
    ("command", "item_type", "evidence_type"),
    [
        ("transcribe-source-item", "audio", "transcript"),
        ("ocr-source-item", "image", "ocr"),
    ],
)
def test_default_media_command_does_not_persist_mock_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    item_type: str,
    evidence_type: str,
) -> None:
    monkeypatch.delenv("QDRANT_URL", raising=False)
    db_path = tmp_path / f"atlas-default-{item_type}.db"
    media_path = tmp_path / f"fixture.{item_type}"
    media_path.write_bytes(b"synthetic media fixture")
    service = MemoryService(db_path, workspace_root=tmp_path)
    service.init_db()
    source = service.upsert_external_source(
        source_type="phase0-test",
        display_name=f"default-{item_type}",
    )
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id=f"fixture-{item_type}",
        item_type=item_type,
        payload_json={"media_path": str(media_path)},
    )

    rc = _run_cli_allowing_required_provider(
        [
            "--db",
            str(db_path),
            "--workspace",
            str(tmp_path),
            command,
            "--source-item-id",
            str(item.id),
        ]
    )

    persisted = service.list_evidence_items(
        source_item_id=item.id,
        evidence_type=evidence_type,
    )
    assert persisted == []
    assert rc != 0
