from __future__ import annotations

import json
from pathlib import Path

from memorymaster.action_extractor import propose_actions_from_evidence
from memorymaster.cli import main
from memorymaster.service import MemoryService


def test_propose_actions_from_whatsapp_evidence_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "atlas.db"
    service = MemoryService(db, workspace_root=tmp_path)
    service.init_db()
    source = service.upsert_external_source(source_type="whatsapp", display_name="primary")
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id="msg-task-1",
        item_type="message",
        chat_id="client-1",
        sender_name="Client",
        occurred_at="2026-05-05T10:00:00-03:00",
        text="Can you send me the installation quote tomorrow?",
    )
    service.add_evidence_item(
        source_item_id=item.id,
        evidence_type="message_text",
        text=item.text,
    )

    first = propose_actions_from_evidence(service)
    second = propose_actions_from_evidence(service)

    assert first.scanned == 1
    assert first.matched == 1
    assert first.created == 1
    assert second.created == 0
    assert second.existing == 1
    proposal = first.proposals[0]
    assert proposal.title == "Send me the installation quote"
    assert proposal.status == "candidate"
    assert proposal.destination == "super-productivity"
    assert proposal.suggested_due_at == "2026-05-06T12:00:00-03:00"


def test_action_proposal_cli_list_and_resolve(tmp_path: Path, capsys) -> None:
    db = tmp_path / "atlas.db"
    export_path = tmp_path / "whatsapp.json"
    export_path.write_text(
        json.dumps([{"id": "wamid.1", "chat_id": "family", "text": "Please pay the fiber bill tomorrow"}]),
        encoding="utf-8",
    )

    assert main(["--db", str(db), "init-db"]) == 0
    assert main(["--db", str(db), "import-whatsapp", "--input", str(export_path)]) == 0
    capsys.readouterr()
    assert main(["--db", str(db), "--json", "propose-actions"]) == 0
    created = json.loads(capsys.readouterr().out.strip())
    proposal_id = created["data"]["proposals"][0]["id"]

    assert main([
        "--db",
        str(db),
        "--json",
        "resolve-action-proposal",
        "--proposal-id",
        str(proposal_id),
        "--status",
        "approved",
    ]) == 0
    approved = json.loads(capsys.readouterr().out.strip())
    assert approved["data"]["status"] == "approved"

    assert main(["--db", str(db), "--json", "action-proposals", "--status", "approved"]) == 0
    listed = json.loads(capsys.readouterr().out.strip())
    assert listed["meta"]["total"] == 1
    assert listed["data"][0]["id"] == proposal_id
