from __future__ import annotations

import json
from pathlib import Path

from memorymaster.bridges.action_exporters import export_approved_actions
from memorymaster.surfaces.cli import main
from memorymaster.core.service import MemoryService


def test_export_approved_actions_writes_bridge_file_and_marks_exported(tmp_path: Path) -> None:
    db = tmp_path / "atlas.db"
    service = MemoryService(db, workspace_root=tmp_path)
    service.init_db()
    proposal = service.create_action_proposal(
        proposal_type="task",
        title="Pay fiber bill",
        description="Source-backed task",
        destination="super-productivity",
        suggested_due_at="2026-05-06T12:00:00-03:00",
        confidence=0.8,
        idempotency_key="proposal-export-1",
    )
    service.update_action_proposal_status(proposal.id, status="approved")
    output_path = tmp_path / "exports" / "super-productivity.json"

    result = export_approved_actions(service, output_path)

    assert result.exported == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["format"] == "atlas-super-productivity-bridge-v1"
    assert payload["tasks"][0]["title"] == "Pay fiber bill"
    assert payload["tasks"][0]["due"] == "2026-05-06T12:00:00-03:00"
    exported = service.list_action_proposals(status="exported", destination="super-productivity")
    assert [row.id for row in exported] == [proposal.id]
    assert exported[0].external_ref is not None


def test_export_actions_cli_json_output(tmp_path: Path, capsys) -> None:
    db = tmp_path / "atlas.db"
    output_path = tmp_path / "sp.json"
    export_path = tmp_path / "whatsapp.json"
    export_path.write_text(
        json.dumps([{"id": "wamid.1", "chat_id": "family", "text": "Please pay the fiber bill tomorrow"}]),
        encoding="utf-8",
    )

    assert main(["--db", str(db), "init-db"]) == 0
    assert main(["--db", str(db), "import-whatsapp", "--input", str(export_path)]) == 0
    assert main(["--db", str(db), "propose-actions"]) == 0
    proposals = MemoryService(db, workspace_root=tmp_path).list_action_proposals(status="candidate")
    assert proposals
    assert main([
        "--db",
        str(db),
        "resolve-action-proposal",
        "--proposal-id",
        str(proposals[0].id),
        "--status",
        "approved",
    ]) == 0
    capsys.readouterr()
    assert main(["--db", str(db), "--json", "export-actions", "--output", str(output_path)]) == 0
    payload = json.loads(capsys.readouterr().out.strip())

    assert payload["ok"] is True
    assert payload["data"]["exported"] == 1
    assert output_path.exists()
