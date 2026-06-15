from __future__ import annotations

import json
from pathlib import Path

from memorymaster.surfaces.cli import main
from memorymaster.bridges.connectors.whatsapp import import_wacli_json
from memorymaster.core.service import MemoryService


def test_wacli_import_creates_source_items_and_text_evidence(tmp_path: Path) -> None:
    db = tmp_path / "atlas.db"
    service = MemoryService(db, workspace_root=tmp_path)
    service.init_db()
    export_path = tmp_path / "whatsapp.json"
    export_path.write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "id": "wamid.1",
                        "chat_id": "family@g.us",
                        "sender_id": "5491111111111@s.whatsapp.net",
                        "sender_name": "Pau",
                        "timestamp": "2026-05-05T10:00:00-03:00",
                        "text": "Pay the fiber bill tomorrow",
                    },
                    {
                        "messageId": "wamid.2",
                        "chatId": "family@g.us",
                        "from": "5492222222222@s.whatsapp.net",
                        "body": "I sent the PDF receipt",
                        "type": "image",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = import_wacli_json(service, export_path, display_name="Main WhatsApp")

    assert result.source_items_seen == 2
    assert result.source_items_imported == 2
    assert result.evidence_items_added == 2
    item = service.get_source_item(source_id=result.source_id, source_item_id="wamid.1")
    assert item is not None
    assert item.chat_id == "family@g.us"
    evidence = service.list_evidence_items(source_item_id=item.id)
    assert len(evidence) == 1
    assert evidence[0].text == "Pay the fiber bill tomorrow"


def test_wacli_import_is_idempotent_for_existing_evidence(tmp_path: Path) -> None:
    db = tmp_path / "atlas.db"
    service = MemoryService(db, workspace_root=tmp_path)
    service.init_db()
    export_path = tmp_path / "whatsapp.jsonl"
    export_path.write_text(
        "\n".join(
            [
                json.dumps({"id": "wamid.1", "chat_id": "work", "text": "Ship the demo"}),
                json.dumps({"id": "wamid.1", "chat_id": "work", "text": "Ship the demo"}),
            ]
        ),
        encoding="utf-8",
    )

    first = import_wacli_json(service, export_path)
    second = import_wacli_json(service, export_path)

    assert first.source_items_seen == 1
    assert first.duplicates_seen == 1
    assert first.evidence_items_added == 1
    assert second.source_items_imported == 0
    assert second.source_items_updated == 1
    assert second.evidence_items_added == 0


def test_import_whatsapp_cli_json_output(tmp_path: Path, capsys) -> None:
    db = tmp_path / "atlas.db"
    export_path = tmp_path / "whatsapp.json"
    export_path.write_text(
        json.dumps([{"id": "wamid.1", "chat_id": "family", "text": "Buy milk"}]),
        encoding="utf-8",
    )

    assert main(["--db", str(db), "init-db"]) == 0
    capsys.readouterr()
    rc = main([
        "--db",
        str(db),
        "--json",
        "import-whatsapp",
        "--input",
        str(export_path),
        "--display-name",
        "Test WhatsApp",
    ])
    out = capsys.readouterr().out.strip()

    assert rc == 0
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["data"]["source_items_imported"] == 1
    assert payload["data"]["evidence_items_added"] == 1
