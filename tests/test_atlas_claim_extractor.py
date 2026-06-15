from __future__ import annotations

import json
from pathlib import Path

from memorymaster.bridges.atlas_claim_extractor import extract_atlas_claims_from_evidence
from memorymaster.surfaces.cli import main
from memorymaster.core.service import MemoryService


def test_extract_atlas_claims_creates_candidate_with_whatsapp_citation(tmp_path: Path) -> None:
    db = tmp_path / "atlas.db"
    service = MemoryService(db, workspace_root=tmp_path)
    service.init_db()
    source = service.upsert_external_source(source_type="whatsapp", display_name="primary")
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id="msg-quote-1",
        item_type="message",
        text="Can you send the installation quote tomorrow?",
    )
    service.add_evidence_item(
        source_item_id=item.id,
        evidence_type="message_text",
        text=item.text,
    )

    first = extract_atlas_claims_from_evidence(service, scope="project:atlas-test")
    second = extract_atlas_claims_from_evidence(service, scope="project:atlas-test")

    assert first.scanned == 1
    assert first.matched == 1
    assert first.ingested == 1
    assert second.ingested == 1
    assert second.claims[0].id == first.claims[0].id
    claim = first.claims[0]
    assert claim.status == "candidate"
    assert claim.claim_type == "request"
    assert claim.predicate == "requested_quote"
    assert claim.scope == "project:atlas-test"
    assert claim.citations[0].source == f"whatsapp://source/{source.id}/item/msg-quote-1"
    assert claim.citations[0].locator == "evidence:1"


def test_extract_atlas_claims_cli_json_output(tmp_path: Path, capsys) -> None:
    db = tmp_path / "atlas.db"
    export_path = tmp_path / "whatsapp.json"
    export_path.write_text(
        json.dumps([{"id": "wamid.1", "chat_id": "support", "text": "The router stopped working yesterday"}]),
        encoding="utf-8",
    )

    assert main(["--db", str(db), "init-db"]) == 0
    assert main(["--db", str(db), "import-whatsapp", "--input", str(export_path)]) == 0
    capsys.readouterr()
    assert main(["--db", str(db), "--json", "extract-atlas-claims", "--scope", "project:atlas-cli"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())

    assert payload["ok"] is True
    assert payload["meta"]["total"] == 1
    assert payload["data"]["claims"][0]["claim_type"] == "problem"
