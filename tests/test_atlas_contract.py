"""Atlas API/CLI contract tests.

These tests pin the JSON envelope shape that LifeAgent (and any other Atlas
frontend) depends on. **If you break a test here, you are making a breaking
change** — bump the major in ``memorymaster/atlas_contract.py:ATLAS_CONTRACT_VERSION``
and add an entry to ``BREAKING_CHANGES_SINCE``.

Adding a NEW field to an envelope is additive — extend the assertion to
include the new key, do not remove old keys.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from memorymaster.bridges.atlas_contract import (
    ATLAS_CONTRACT_NAME,
    ATLAS_CONTRACT_VERSION,
    ATLAS_ENDPOINTS,
    ATLAS_SUBCOMMANDS,
    atlas_contract_payload,
    atlas_meta,
)
from memorymaster.surfaces.cli import main


# ---------------------------------------------------------------------------
# Static contract assertions
# ---------------------------------------------------------------------------


def test_contract_version_is_semver() -> None:
    parts = ATLAS_CONTRACT_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts), f"non-numeric semver part in {ATLAS_CONTRACT_VERSION}"


def test_contract_name_is_stable() -> None:
    assert ATLAS_CONTRACT_NAME == "atlas-inbox-v1"


def test_atlas_meta_shape() -> None:
    meta = atlas_meta("import-whatsapp")
    assert set(meta.keys()) == {"atlas_contract_version", "atlas_subcommand"}
    assert meta["atlas_contract_version"] == ATLAS_CONTRACT_VERSION
    assert meta["atlas_subcommand"] == "import-whatsapp"


def test_atlas_contract_payload_shape() -> None:
    payload = atlas_contract_payload()
    assert set(payload.keys()) == {
        "atlas_contract_version",
        "atlas_contract_name",
        "subcommands",
        "endpoints",
        "breaking_changes_since",
    }
    assert payload["atlas_contract_version"] == ATLAS_CONTRACT_VERSION


@pytest.mark.parametrize("name", [
    "init-db",
    "import-whatsapp",
    "extract-atlas-claims",
    "propose-actions",
    "action-proposals",
    "resolve-action-proposal",
    "edit-action-proposal",
    "label-source-item",
    "label-evidence-item",
    "enqueue-media-retry",
    "process-media-retry-queue",
    "record-media-retry-outcome",
    "list-media-retries",
    "transcribe-source-item",
    "ocr-source-item",
    "export-actions",
    "atlas-version",
])
def test_subcommand_listed_in_contract(name: str) -> None:
    """Every Atlas CLI subcommand MUST be declared in the contract."""
    listed = {sc["name"] for sc in ATLAS_SUBCOMMANDS}
    assert name in listed


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/action-proposals"),
    ("POST", "/api/action-proposals/status"),
    ("GET", "/api/atlas/version"),
])
def test_endpoint_listed_in_contract(method: str, path: str) -> None:
    """Every Atlas dashboard endpoint MUST be declared in the contract."""
    listed = {(ep["method"], ep["path"]) for ep in ATLAS_ENDPOINTS}
    assert (method, path) in listed


def test_subcommand_entries_have_required_keys() -> None:
    for sc in ATLAS_SUBCOMMANDS:
        assert set(sc.keys()) >= {"name", "description", "inputs", "data_keys", "meta_total"}, sc


def test_endpoint_entries_have_required_keys() -> None:
    for ep in ATLAS_ENDPOINTS:
        assert set(ep.keys()) >= {"method", "path", "description", "response_keys"}, ep


# ---------------------------------------------------------------------------
# CLI envelope shape — golden assertions
# ---------------------------------------------------------------------------


def _run_cli(*args: str, capsys) -> dict:
    capsys.readouterr()
    rc = main(list(args))
    assert rc == 0, f"CLI failed: {' '.join(args)}"
    return json.loads(capsys.readouterr().out.strip())


def _assert_envelope(env: dict, *, subcommand: str) -> None:
    """Pin the envelope shape every Atlas CLI subcommand must produce."""
    assert set(env.keys()) >= {"ok", "data", "meta"}
    assert env["ok"] is True
    assert env["meta"]["atlas_contract_version"] == ATLAS_CONTRACT_VERSION
    assert env["meta"]["atlas_subcommand"] == subcommand
    assert "query_ms" in env["meta"]


def test_atlas_version_cli_envelope(tmp_path: Path, capsys) -> None:
    db = tmp_path / "atlas.db"
    main(["--db", str(db), "init-db"])
    capsys.readouterr()
    env = _run_cli("--db", str(db), "--json", "atlas-version", capsys=capsys)
    _assert_envelope(env, subcommand="atlas-version")
    assert env["data"]["atlas_contract_version"] == ATLAS_CONTRACT_VERSION
    assert env["data"]["atlas_contract_name"] == ATLAS_CONTRACT_NAME
    assert isinstance(env["data"]["subcommands"], list)
    assert isinstance(env["data"]["endpoints"], list)


def _seed_whatsapp(tmp_path: Path) -> tuple[Path, Path]:
    db = tmp_path / "atlas.db"
    export = tmp_path / "wa.json"
    export.write_text(
        json.dumps([
            {"id": "wamid.1", "chat_id": "client", "text": "Can you send the installation quote tomorrow?"},
        ]),
        encoding="utf-8",
    )
    main(["--db", str(db), "init-db"])
    return db, export


def test_import_whatsapp_envelope(tmp_path: Path, capsys) -> None:
    db, export = _seed_whatsapp(tmp_path)
    capsys.readouterr()
    env = _run_cli("--db", str(db), "--json", "import-whatsapp", "--input", str(export), capsys=capsys)
    _assert_envelope(env, subcommand="import-whatsapp")
    assert set(env["data"].keys()) >= {
        "source_id",
        "source_items_seen",
        "source_items_imported",
        "source_items_updated",
        "evidence_items_added",
        "duplicates_seen",
    }


def test_extract_atlas_claims_envelope(tmp_path: Path, capsys) -> None:
    db, export = _seed_whatsapp(tmp_path)
    main(["--db", str(db), "import-whatsapp", "--input", str(export)])
    capsys.readouterr()
    env = _run_cli(
        "--db", str(db), "--json", "extract-atlas-claims", "--scope", "project:atlas-test",
        capsys=capsys,
    )
    _assert_envelope(env, subcommand="extract-atlas-claims")
    assert set(env["data"].keys()) >= {"scanned", "matched", "ingested", "claims"}


def test_propose_actions_envelope(tmp_path: Path, capsys) -> None:
    db, export = _seed_whatsapp(tmp_path)
    main(["--db", str(db), "import-whatsapp", "--input", str(export)])
    capsys.readouterr()
    env = _run_cli("--db", str(db), "--json", "propose-actions", capsys=capsys)
    _assert_envelope(env, subcommand="propose-actions")
    assert set(env["data"].keys()) >= {"scanned", "matched", "created", "existing", "proposals"}


def test_action_proposals_list_envelope(tmp_path: Path, capsys) -> None:
    db, export = _seed_whatsapp(tmp_path)
    main(["--db", str(db), "import-whatsapp", "--input", str(export)])
    main(["--db", str(db), "propose-actions"])
    capsys.readouterr()
    env = _run_cli("--db", str(db), "--json", "action-proposals", capsys=capsys)
    _assert_envelope(env, subcommand="action-proposals")
    assert isinstance(env["data"], list)
    assert env["data"], "expected at least one proposal in list"
    proposal = env["data"][0]
    expected_proposal_keys = {
        "id",
        "proposal_type",
        "title",
        "description",
        "source_item_id",
        "evidence_item_id",
        "claim_id",
        "suggested_due_at",
        "destination",
        "status",
        "confidence",
        "payload_json",
        "exported_at",
        "external_ref",
        "idempotency_key",
        "created_at",
        "updated_at",
    }
    assert set(proposal.keys()) >= expected_proposal_keys, (
        f"missing proposal keys: {expected_proposal_keys - set(proposal.keys())}"
    )


def test_resolve_action_proposal_envelope(tmp_path: Path, capsys) -> None:
    db, export = _seed_whatsapp(tmp_path)
    main(["--db", str(db), "import-whatsapp", "--input", str(export)])
    main(["--db", str(db), "propose-actions"])
    # Get the proposal id via the JSON list
    capsys.readouterr()
    main(["--db", str(db), "--json", "action-proposals"])
    listed = json.loads(capsys.readouterr().out.strip())
    proposal_id = listed["data"][0]["id"]
    capsys.readouterr()
    env = _run_cli(
        "--db", str(db), "--json", "resolve-action-proposal",
        "--proposal-id", str(proposal_id), "--status", "approved",
        capsys=capsys,
    )
    _assert_envelope(env, subcommand="resolve-action-proposal")
    assert env["data"]["status"] == "approved"
    assert env["data"]["id"] == proposal_id


def test_export_actions_envelope(tmp_path: Path, capsys) -> None:
    db, export = _seed_whatsapp(tmp_path)
    main(["--db", str(db), "import-whatsapp", "--input", str(export)])
    main(["--db", str(db), "propose-actions"])
    capsys.readouterr()
    main(["--db", str(db), "--json", "action-proposals"])
    listed = json.loads(capsys.readouterr().out.strip())
    proposal_id = listed["data"][0]["id"]
    main([
        "--db", str(db), "resolve-action-proposal",
        "--proposal-id", str(proposal_id), "--status", "approved",
    ])
    output = tmp_path / "sp.json"
    capsys.readouterr()
    env = _run_cli(
        "--db", str(db), "--json", "export-actions",
        "--output", str(output),
        capsys=capsys,
    )
    _assert_envelope(env, subcommand="export-actions")
    assert set(env["data"].keys()) >= {"destination", "output_path", "exported", "proposal_ids"}
    assert env["data"]["exported"] >= 1
    assert output.exists()
    sp_payload = json.loads(output.read_text(encoding="utf-8"))
    # Lock the Super-Productivity bridge JSON shape too — this is the file
    # LifeAgent or Super Productivity will consume.
    assert set(sp_payload.keys()) >= {"format", "destination", "tasks"}
    assert sp_payload["format"] == "atlas-super-productivity-bridge-v1"
    assert sp_payload["tasks"], "expected at least one exported task"
    task = sp_payload["tasks"][0]
    assert set(task.keys()) >= {
        "title",
        "notes",
        "due",
        "atlas_proposal_id",
        "atlas_confidence",
        "atlas_payload",
    }


# ---------------------------------------------------------------------------
# Dashboard endpoint shape — service-level (no live HTTP)
# ---------------------------------------------------------------------------


def test_atlas_version_endpoint_payload_matches_cli() -> None:
    """The dashboard endpoint MUST return the same payload as the CLI."""
    payload = atlas_contract_payload()
    assert payload["atlas_contract_version"] == ATLAS_CONTRACT_VERSION
    assert payload["atlas_contract_name"] == ATLAS_CONTRACT_NAME
    listed_endpoints = {(ep["method"], ep["path"]) for ep in payload["endpoints"]}
    assert ("GET", "/api/atlas/version") in listed_endpoints


# ---------------------------------------------------------------------------
# init-db envelope
# ---------------------------------------------------------------------------


def test_init_db_envelope(tmp_path: Path, capsys) -> None:
    db = tmp_path / "atlas.db"
    capsys.readouterr()
    env = _run_cli("--db", str(db), "--json", "init-db", capsys=capsys)
    _assert_envelope(env, subcommand="init-db")
    assert set(env["data"].keys()) >= {"db", "stealth"}
    assert env["data"]["db"].endswith("atlas.db")
    assert db.exists()


# ---------------------------------------------------------------------------
# WhatsApp fixture-based contract test
# ---------------------------------------------------------------------------


_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "atlas"


def test_whatsapp_basic_fixture_full_pipeline(tmp_path: Path, capsys) -> None:
    """End-to-end contract test using the canonical WhatsApp wacli fixture.

    LifeAgent and other consumers should be able to mirror this fixture in
    their own test suite to lock the import → extract → propose → list →
    resolve → export pipeline. Any breakage here is a contract break.
    """
    fixture = _FIXTURE_DIR / "whatsapp_wacli_basic.json"
    assert fixture.exists(), f"fixture missing: {fixture}"
    db = tmp_path / "atlas.db"
    main(["--db", str(db), "init-db"])

    # Import → must dedupe the trailing duplicate row
    capsys.readouterr()
    env_import = _run_cli(
        "--db", str(db), "--json", "import-whatsapp", "--input", str(fixture),
        capsys=capsys,
    )
    _assert_envelope(env_import, subcommand="import-whatsapp")
    data = env_import["data"]
    assert data["source_items_seen"] == 5, "fixture has 6 raw rows but 1 is a dupe"
    assert data["source_items_imported"] == 5
    assert data["duplicates_seen"] == 1

    # Extract → 4 of 5 evidence items match an extractor template (3 spanish text + 1 with caption text? wait the audio has no text yet)
    capsys.readouterr()
    env_extract = _run_cli(
        "--db", str(db), "--json", "extract-atlas-claims", "--scope", "project:atlas-fixture",
        capsys=capsys,
    )
    _assert_envelope(env_extract, subcommand="extract-atlas-claims")
    assert env_extract["data"]["scanned"] >= 3, "expected at least 3 text-bearing evidence items scanned"
    assert env_extract["data"]["matched"] >= 1, "expected at least one extractor match"

    # Propose → at least 1 action (the 'recordame' message)
    capsys.readouterr()
    env_propose = _run_cli(
        "--db", str(db), "--json", "propose-actions",
        capsys=capsys,
    )
    _assert_envelope(env_propose, subcommand="propose-actions")
    assert env_propose["data"]["created"] >= 1

    # List
    capsys.readouterr()
    env_list = _run_cli("--db", str(db), "--json", "action-proposals", capsys=capsys)
    _assert_envelope(env_list, subcommand="action-proposals")
    assert isinstance(env_list["data"], list)
    assert env_list["data"], "expected at least one proposal"
    proposal_id = env_list["data"][0]["id"]

    # Resolve
    capsys.readouterr()
    env_resolve = _run_cli(
        "--db", str(db), "--json", "resolve-action-proposal",
        "--proposal-id", str(proposal_id), "--status", "approved",
        capsys=capsys,
    )
    _assert_envelope(env_resolve, subcommand="resolve-action-proposal")
    assert env_resolve["data"]["status"] == "approved"

    # Export
    output = tmp_path / "sp.json"
    capsys.readouterr()
    env_export = _run_cli(
        "--db", str(db), "--json", "export-actions", "--output", str(output),
        capsys=capsys,
    )
    _assert_envelope(env_export, subcommand="export-actions")
    assert env_export["data"]["exported"] >= 1
    assert output.exists()
    sp = json.loads(output.read_text(encoding="utf-8"))
    assert sp["format"] == "atlas-super-productivity-bridge-v1"
    assert sp["tasks"], "expected exported task in bridge file"


def test_edit_action_proposal_envelope(tmp_path: Path, capsys) -> None:
    fixture = _FIXTURE_DIR / "whatsapp_wacli_basic.json"
    db = tmp_path / "atlas.db"
    main(["--db", str(db), "init-db"])
    main(["--db", str(db), "import-whatsapp", "--input", str(fixture)])
    main(["--db", str(db), "propose-actions"])
    capsys.readouterr()
    main(["--db", str(db), "--json", "action-proposals"])
    listed = json.loads(capsys.readouterr().out.strip())
    proposal_id = listed["data"][0]["id"]
    original_status = listed["data"][0]["status"]

    capsys.readouterr()
    env = _run_cli(
        "--db", str(db), "--json", "edit-action-proposal",
        "--proposal-id", str(proposal_id),
        "--title", "Renamed by LifeAgent",
        "--description", "Updated description",
        "--suggested-due-at", "2026-06-01T09:00:00-03:00",
        "--confidence", "0.85",
        capsys=capsys,
    )
    _assert_envelope(env, subcommand="edit-action-proposal")
    assert env["data"]["id"] == proposal_id
    assert env["data"]["title"] == "Renamed by LifeAgent"
    assert env["data"]["description"] == "Updated description"
    assert env["data"]["suggested_due_at"] == "2026-06-01T09:00:00-03:00"
    assert env["data"]["confidence"] == 0.85
    # Critical: lifecycle fields MUST be untouched
    assert env["data"]["status"] == original_status
    assert env["data"]["external_ref"] is None
    assert env["data"]["exported_at"] is None


def test_edit_action_proposal_partial_update(tmp_path: Path, capsys) -> None:
    """Partial edits leave omitted fields untouched."""
    fixture = _FIXTURE_DIR / "whatsapp_wacli_basic.json"
    db = tmp_path / "atlas.db"
    main(["--db", str(db), "init-db"])
    main(["--db", str(db), "import-whatsapp", "--input", str(fixture)])
    main(["--db", str(db), "propose-actions"])
    capsys.readouterr()
    main(["--db", str(db), "--json", "action-proposals"])
    listed = json.loads(capsys.readouterr().out.strip())
    original = listed["data"][0]
    proposal_id = original["id"]

    capsys.readouterr()
    env = _run_cli(
        "--db", str(db), "--json", "edit-action-proposal",
        "--proposal-id", str(proposal_id),
        "--description", "Only description changed",
        capsys=capsys,
    )
    _assert_envelope(env, subcommand="edit-action-proposal")
    assert env["data"]["description"] == "Only description changed"
    assert env["data"]["title"] == original["title"]
    assert env["data"]["suggested_due_at"] == original["suggested_due_at"]
    assert env["data"]["confidence"] == original["confidence"]


def test_edit_action_proposal_records_audit_event(tmp_path: Path) -> None:
    """Every successful edit MUST record an action_proposal event."""
    from memorymaster.surfaces.cli import main as cli_main
    from memorymaster.service import MemoryService
    fixture = _FIXTURE_DIR / "whatsapp_wacli_basic.json"
    db = tmp_path / "atlas.db"
    cli_main(["--db", str(db), "init-db"])
    cli_main(["--db", str(db), "import-whatsapp", "--input", str(fixture)])
    cli_main(["--db", str(db), "propose-actions"])

    service = MemoryService(db, workspace_root=tmp_path)
    proposals = service.list_action_proposals()
    assert proposals, "expected proposal from fixture"
    proposal = proposals[0]
    events_before = service.list_events(event_type="action_proposal")
    n_before = len(events_before)

    service.update_action_proposal_fields(proposal.id, title="Audited edit")
    events_after = service.list_events(event_type="action_proposal")
    assert len(events_after) == n_before + 1, "expected one new action_proposal event"
    new_event = events_after[0]  # newest first
    assert new_event.details == "action_proposal_fields_updated"
    payload = json.loads(new_event.payload_json or "{}")
    assert payload["proposal_id"] == proposal.id
    assert "title" in payload["changed"]


def test_edit_action_proposal_noop_records_no_event(tmp_path: Path) -> None:
    """Edit with all fields equal to current values is a no-op (no event)."""
    from memorymaster.surfaces.cli import main as cli_main
    from memorymaster.service import MemoryService
    fixture = _FIXTURE_DIR / "whatsapp_wacli_basic.json"
    db = tmp_path / "atlas.db"
    cli_main(["--db", str(db), "init-db"])
    cli_main(["--db", str(db), "import-whatsapp", "--input", str(fixture)])
    cli_main(["--db", str(db), "propose-actions"])

    service = MemoryService(db, workspace_root=tmp_path)
    proposal = service.list_action_proposals()[0]
    n_before = len(service.list_events(event_type="action_proposal"))
    service.update_action_proposal_fields(proposal.id, title=proposal.title)
    n_after = len(service.list_events(event_type="action_proposal"))
    assert n_after == n_before, "no-op edit must not record an event"


def test_edit_action_proposal_rejects_blank_title(tmp_path: Path) -> None:
    from memorymaster.surfaces.cli import main as cli_main
    from memorymaster.service import MemoryService
    fixture = _FIXTURE_DIR / "whatsapp_wacli_basic.json"
    db = tmp_path / "atlas.db"
    cli_main(["--db", str(db), "init-db"])
    cli_main(["--db", str(db), "import-whatsapp", "--input", str(fixture)])
    cli_main(["--db", str(db), "propose-actions"])
    service = MemoryService(db, workspace_root=tmp_path)
    proposal = service.list_action_proposals()[0]
    with pytest.raises(ValueError, match="title cannot be blank"):
        service.update_action_proposal_fields(proposal.id, title="   ")


def test_edit_action_proposal_rejects_no_fields(tmp_path: Path) -> None:
    from memorymaster.surfaces.cli import main as cli_main
    from memorymaster.service import MemoryService
    fixture = _FIXTURE_DIR / "whatsapp_wacli_basic.json"
    db = tmp_path / "atlas.db"
    cli_main(["--db", str(db), "init-db"])
    cli_main(["--db", str(db), "import-whatsapp", "--input", str(fixture)])
    cli_main(["--db", str(db), "propose-actions"])
    service = MemoryService(db, workspace_root=tmp_path)
    proposal = service.list_action_proposals()[0]
    with pytest.raises(ValueError, match="at least one field"):
        service.update_action_proposal_fields(proposal.id)


def _seed_with_proposal(tmp_path: Path) -> tuple[Path, int, int]:
    fixture = _FIXTURE_DIR / "whatsapp_wacli_basic.json"
    db = tmp_path / "atlas.db"
    main(["--db", str(db), "init-db"])
    main(["--db", str(db), "import-whatsapp", "--input", str(fixture)])
    from memorymaster.service import MemoryService
    svc = MemoryService(db, workspace_root=tmp_path)
    items = svc.store.connect()
    with svc.store.connect() as conn:
        rows = conn.execute("SELECT id FROM source_items LIMIT 1").fetchall()
        source_item_id = int(rows[0]["id"])
        rows = conn.execute("SELECT id FROM evidence_items LIMIT 1").fetchall()
        evidence_item_id = int(rows[0]["id"])
    return db, source_item_id, evidence_item_id


def test_label_source_item_envelope(tmp_path: Path, capsys) -> None:
    db, sid, _ = _seed_with_proposal(tmp_path)
    capsys.readouterr()
    env = _run_cli(
        "--db", str(db), "--json", "label-source-item",
        "--source-item-id", str(sid),
        "--sensitivity", "high",
        capsys=capsys,
    )
    _assert_envelope(env, subcommand="label-source-item")
    assert env["data"]["id"] == sid
    assert env["data"]["sensitivity"] == "high"


def test_label_evidence_item_envelope(tmp_path: Path, capsys) -> None:
    db, _, eid = _seed_with_proposal(tmp_path)
    capsys.readouterr()
    env = _run_cli(
        "--db", str(db), "--json", "label-evidence-item",
        "--evidence-item-id", str(eid),
        "--sensitivity", "redacted",
        capsys=capsys,
    )
    _assert_envelope(env, subcommand="label-evidence-item")
    assert env["data"]["id"] == eid
    assert env["data"]["sensitivity"] == "redacted"


def test_label_clear_resets_to_null(tmp_path: Path, capsys) -> None:
    db, sid, _ = _seed_with_proposal(tmp_path)
    main([
        "--db", str(db), "label-source-item",
        "--source-item-id", str(sid), "--sensitivity", "medium",
    ])
    capsys.readouterr()
    env = _run_cli(
        "--db", str(db), "--json", "label-source-item",
        "--source-item-id", str(sid), "--sensitivity", "clear",
        capsys=capsys,
    )
    _assert_envelope(env, subcommand="label-source-item")
    assert env["data"]["sensitivity"] is None


def test_sensitivity_preserved_on_reimport(tmp_path: Path) -> None:
    """Re-importing a fixture must NOT wipe operator-applied labels."""
    from memorymaster.service import MemoryService
    fixture = _FIXTURE_DIR / "whatsapp_wacli_basic.json"
    db = tmp_path / "atlas.db"
    main(["--db", str(db), "init-db"])
    main(["--db", str(db), "import-whatsapp", "--input", str(fixture)])
    svc = MemoryService(db, workspace_root=tmp_path)
    item = svc.list_evidence_items(limit=1)[0]
    source_item = svc.get_source_item_by_id(item.source_item_id)
    svc.set_source_item_sensitivity(source_item.id, "high")

    main(["--db", str(db), "import-whatsapp", "--input", str(fixture)])
    refreshed = svc.get_source_item_by_id(source_item.id)
    assert refreshed.sensitivity == "high", "import must preserve operator label"


def test_sensitivity_rejects_invalid_value(tmp_path: Path) -> None:
    from memorymaster.service import MemoryService
    db, sid, _ = _seed_with_proposal(tmp_path)
    svc = MemoryService(db, workspace_root=tmp_path)
    with pytest.raises(ValueError, match="sensitivity must be one of"):
        svc.set_source_item_sensitivity(sid, "extreme")


def test_sensitivity_records_audit_event_on_change(tmp_path: Path) -> None:
    from memorymaster.service import MemoryService
    db, sid, _ = _seed_with_proposal(tmp_path)
    svc = MemoryService(db, workspace_root=tmp_path)
    n_before = len(svc.list_events(event_type="source_import"))
    svc.set_source_item_sensitivity(sid, "low")
    n_after = len(svc.list_events(event_type="source_import"))
    assert n_after == n_before + 1
    new_event = svc.list_events(event_type="source_import")[0]  # newest first
    assert new_event.details == "source_item_sensitivity_set"
    payload = json.loads(new_event.payload_json or "{}")
    assert payload["to"] == "low"


def test_sensitivity_no_event_on_noop(tmp_path: Path) -> None:
    from memorymaster.service import MemoryService
    db, sid, _ = _seed_with_proposal(tmp_path)
    svc = MemoryService(db, workspace_root=tmp_path)
    svc.set_source_item_sensitivity(sid, "low")
    n_before = len(svc.list_events(event_type="source_import"))
    svc.set_source_item_sensitivity(sid, "low")  # no-op
    n_after = len(svc.list_events(event_type="source_import"))
    assert n_after == n_before, "no-op label set must not record event"


# ---------------------------------------------------------------------------
# Media retry queue (v1.4.0)
# ---------------------------------------------------------------------------


def _seed_with_source_item(tmp_path: Path) -> tuple[Path, int]:
    fixture = _FIXTURE_DIR / "whatsapp_wacli_basic.json"
    db = tmp_path / "atlas.db"
    main(["--db", str(db), "init-db"])
    main(["--db", str(db), "import-whatsapp", "--input", str(fixture)])
    from memorymaster.service import MemoryService
    svc = MemoryService(db, workspace_root=tmp_path)
    with svc.store.connect() as conn:
        rows = conn.execute("SELECT id FROM source_items WHERE item_type='audio' LIMIT 1").fetchall()
        assert rows, "expected an audio source_item from fixture"
        return db, int(rows[0]["id"])


def test_enqueue_media_retry_envelope(tmp_path: Path, capsys) -> None:
    db, sid = _seed_with_source_item(tmp_path)
    capsys.readouterr()
    env = _run_cli(
        "--db", str(db), "--json", "enqueue-media-retry",
        "--source-item-id", str(sid),
        "--media-key", "wamid.audio.test1",
        "--media-type", "audio",
        "--media-url", "https://example/m1.ogg",
        capsys=capsys,
    )
    _assert_envelope(env, subcommand="enqueue-media-retry")
    expected = {
        "id", "source_item_id", "media_key", "chat_id", "media_type",
        "media_path", "media_url", "status", "attempt_count",
        "last_http_status", "last_error", "next_attempt_time",
        "created_at", "updated_at",
    }
    assert set(env["data"].keys()) >= expected
    assert env["data"]["status"] == "pending"
    assert env["data"]["attempt_count"] == 0


def test_enqueue_idempotent_does_not_increment_attempts(tmp_path: Path, capsys) -> None:
    db, sid = _seed_with_source_item(tmp_path)
    main([
        "--db", str(db), "enqueue-media-retry",
        "--source-item-id", str(sid), "--media-key", "k1", "--media-type", "audio",
    ])
    capsys.readouterr()
    env = _run_cli(
        "--db", str(db), "--json", "enqueue-media-retry",
        "--source-item-id", str(sid), "--media-key", "k1", "--chat-id", "updated-chat",
        capsys=capsys,
    )
    _assert_envelope(env, subcommand="enqueue-media-retry")
    assert env["data"]["attempt_count"] == 0
    assert env["data"]["chat_id"] == "updated-chat"


def test_process_media_retry_queue_envelope(tmp_path: Path, capsys) -> None:
    db, sid = _seed_with_source_item(tmp_path)
    main([
        "--db", str(db), "enqueue-media-retry",
        "--source-item-id", str(sid), "--media-key", "k-process", "--media-type", "audio",
    ])
    capsys.readouterr()
    env = _run_cli(
        "--db", str(db), "--json", "process-media-retry-queue", "--limit", "10",
        capsys=capsys,
    )
    _assert_envelope(env, subcommand="process-media-retry-queue")
    expected = {"attempted", "expired", "recovered", "failed", "pending_remaining", "rows"}
    assert set(env["data"].keys()) >= expected
    assert env["data"]["attempted"] == 1
    assert env["data"]["rows"][0]["status"] == "retrying"
    assert env["data"]["rows"][0]["attempt_count"] == 1


def test_record_outcome_expired_requires_no_path(tmp_path: Path, capsys) -> None:
    """HTTP 410 / 403 = terminal expired. Must not require media_path."""
    db, sid = _seed_with_source_item(tmp_path)
    main([
        "--db", str(db), "enqueue-media-retry",
        "--source-item-id", str(sid), "--media-key", "k-expired", "--media-type", "audio",
    ])
    main(["--db", str(db), "process-media-retry-queue"])
    capsys.readouterr()
    main(["--db", str(db), "--json", "list-media-retries", "--status", "retrying"])
    listed = json.loads(capsys.readouterr().out.strip())
    retry_id = listed["data"][0]["id"]
    capsys.readouterr()
    env = _run_cli(
        "--db", str(db), "--json", "record-media-retry-outcome",
        "--retry-id", str(retry_id),
        "--status", "expired",
        "--last-http-status", "410",
        "--last-error", "Gone",
        capsys=capsys,
    )
    _assert_envelope(env, subcommand="record-media-retry-outcome")
    assert env["data"]["status"] == "expired"
    assert env["data"]["last_http_status"] == 410


def test_record_outcome_done_requires_media_path(tmp_path: Path) -> None:
    from memorymaster.service import MemoryService
    db, sid = _seed_with_source_item(tmp_path)
    svc = MemoryService(db, workspace_root=tmp_path)
    item = svc.enqueue_media_retry(source_item_id=sid, media_key="k-done", media_type="audio")
    svc.claim_pending_media_retries()
    with pytest.raises(ValueError, match="media_path is required"):
        svc.record_media_retry_outcome(item.id, status="done")


def test_text_import_unaffected_by_media_failure(tmp_path: Path, capsys) -> None:
    """Critical: text/source import must work even if media isn't fetchable.

    The wacli fixture has 3 text rows + 1 audio + 1 image + 1 dupe. Text rows
    must be importable and queryable regardless of media retry state.
    """
    db, _ = _seed_with_source_item(tmp_path)
    capsys.readouterr()
    env = _run_cli("--db", str(db), "--json", "extract-atlas-claims",
                   "--scope", "project:atlas-test", capsys=capsys)
    _assert_envelope(env, subcommand="extract-atlas-claims")
    # Text-only evidence still produces matched claims; media retry state is independent
    assert env["data"]["scanned"] >= 3, "text evidence must be scanned regardless of media state"


def test_list_media_retries_filters_by_status(tmp_path: Path, capsys) -> None:
    db, sid = _seed_with_source_item(tmp_path)
    main(["--db", str(db), "enqueue-media-retry", "--source-item-id", str(sid),
          "--media-key", "k-a", "--media-type", "audio"])
    main(["--db", str(db), "enqueue-media-retry", "--source-item-id", str(sid),
          "--media-key", "k-b", "--media-type", "audio"])
    main(["--db", str(db), "process-media-retry-queue", "--limit", "10"])
    capsys.readouterr()
    env_pending = _run_cli("--db", str(db), "--json", "list-media-retries",
                           "--status", "pending", capsys=capsys)
    env_retrying = _run_cli("--db", str(db), "--json", "list-media-retries",
                            "--status", "retrying", capsys=capsys)
    assert len(env_pending["data"]) == 0
    assert len(env_retrying["data"]) == 2


# ---------------------------------------------------------------------------
# Stale-DB migration (v1.5.1 fix for LifeAgent's "no such column: sensitivity")
# ---------------------------------------------------------------------------


def test_init_db_migrates_stale_atlas_db_without_sensitivity_column(tmp_path: Path) -> None:
    """Regression for LifeAgent's blocker: existing Atlas DB from PR #20 era
    (no sensitivity column) must migrate cleanly via init-db.

    The bug (v1.5.0): schema.sql had CREATE INDEX ON source_items(sensitivity)
    which fired before the ALTER TABLE migration in _ensure_atlas_source_schema.
    On stale DBs the index creation hit "no such column: sensitivity" and
    fell into storage.py's lenient fallback (silently swallowing all DDL errors).
    The fix (v1.5.1): sensitivity indexes moved out of schema.sql; the
    _ensure_atlas_source_schema migration runs ALTER first, then indexes.
    """
    import sqlite3
    db = tmp_path / "atlas-stale.db"
    pre_sensitivity_schema = """
    CREATE TABLE external_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_type TEXT NOT NULL,
        display_name TEXT NOT NULL, config_json TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE (source_type, display_name)
    );
    CREATE TABLE source_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
        source_item_id TEXT NOT NULL, item_type TEXT NOT NULL,
        chat_id TEXT, sender_id TEXT, sender_name TEXT, occurred_at TEXT,
        text TEXT, payload_json TEXT, content_hash TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        FOREIGN KEY (source_id) REFERENCES external_sources(id) ON DELETE CASCADE,
        UNIQUE (source_id, source_item_id)
    );
    CREATE TABLE evidence_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_item_id INTEGER NOT NULL,
        evidence_type TEXT NOT NULL, text TEXT, media_path TEXT,
        provider TEXT, confidence REAL, payload_json TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (source_item_id) REFERENCES source_items(id) ON DELETE CASCADE
    );
    CREATE TABLE action_proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_type TEXT NOT NULL,
        title TEXT NOT NULL, description TEXT,
        source_item_id INTEGER, evidence_item_id INTEGER, claim_id INTEGER,
        suggested_due_at TEXT, destination TEXT NOT NULL DEFAULT 'manual',
        status TEXT NOT NULL DEFAULT 'candidate',
        confidence REAL NOT NULL DEFAULT 0.5,
        payload_json TEXT, exported_at TEXT, external_ref TEXT,
        idempotency_key TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    INSERT INTO external_sources (source_type, display_name, created_at, updated_at)
        VALUES ('whatsapp', 'primary', '2026-05-04T00:00:00Z', '2026-05-04T00:00:00Z');
    INSERT INTO source_items (source_id, source_item_id, item_type, text, created_at, updated_at)
        VALUES (1, 'msg-pre', 'message', 'pre-existing data', '2026-05-04T00:00:00Z', '2026-05-04T00:00:00Z');
    """
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(pre_sensitivity_schema)
        conn.commit()
    finally:
        conn.close()

    from memorymaster.service import MemoryService
    svc = MemoryService(db, workspace_root=tmp_path)
    svc.init_db()  # MUST not raise

    conn = sqlite3.connect(str(db))
    try:
        cols_src = {row[1] for row in conn.execute("PRAGMA table_info(source_items)").fetchall()}
        cols_ev = {row[1] for row in conn.execute("PRAGMA table_info(evidence_items)").fetchall()}
        assert "sensitivity" in cols_src
        assert "sensitivity" in cols_ev
        rows = conn.execute("SELECT id, source_item_id, text FROM source_items").fetchall()
        assert rows == [(1, "msg-pre", "pre-existing data")]
        idx_names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
        assert "idx_source_items_sensitivity" in idx_names
        assert "idx_evidence_items_sensitivity" in idx_names
    finally:
        conn.close()

    # Idempotent: re-run on already-migrated DB.
    svc.init_db()


# ---------------------------------------------------------------------------
# Real provider adapters (v1.5.0)
# ---------------------------------------------------------------------------


def test_provider_factory_returns_mock() -> None:
    from memorymaster.bridges.media_providers import get_ocr_provider, get_transcription_provider
    assert get_transcription_provider("mock").provider_name == "mock-transcription"
    assert get_ocr_provider("mock").provider_name == "mock-ocr"


def test_provider_factory_rejects_unknown() -> None:
    from memorymaster.bridges.media_providers import get_ocr_provider, get_transcription_provider
    with pytest.raises(ValueError, match="Unknown transcription provider"):
        get_transcription_provider("nonexistent")
    with pytest.raises(ValueError, match="Unknown OCR provider"):
        get_ocr_provider("nonexistent")


def test_openai_whisper_class_lazy_imports() -> None:
    """Class must instantiate without OPENAI_API_KEY in env (lazy validation)."""
    import os
    saved = os.environ.pop("OPENAI_API_KEY", None)
    try:
        from memorymaster.bridges.media_providers import OpenAIWhisperTranscriptionProvider
        provider = OpenAIWhisperTranscriptionProvider()
        assert provider.provider_name == "openai-whisper"
        with pytest.raises(RuntimeError, match="requires OPENAI_API_KEY"):
            provider.transcribe("/nonexistent/path.mp3")
    finally:
        if saved is not None:
            os.environ["OPENAI_API_KEY"] = saved


def test_tesseract_class_lazy_imports() -> None:
    """Class must instantiate without pytesseract installed; check is lazy."""
    from memorymaster.bridges.media_providers import TesseractOcrProvider
    provider = TesseractOcrProvider()
    assert provider.provider_name == "tesseract"


def test_transcribe_source_item_mock_envelope(tmp_path: Path, capsys) -> None:
    fixture = _FIXTURE_DIR / "whatsapp_wacli_basic.json"
    db = tmp_path / "atlas.db"
    main(["--db", str(db), "init-db"])
    main(["--db", str(db), "import-whatsapp", "--input", str(fixture)])
    from memorymaster.service import MemoryService
    svc = MemoryService(db, workspace_root=tmp_path)
    audio_item = next((i for i in svc.list_evidence_items() if i.evidence_type == "message_text"), None)
    # Find the audio source_item
    with svc.store.connect() as conn:
        rows = conn.execute("SELECT id FROM source_items WHERE item_type='audio' LIMIT 1").fetchall()
        sid = int(rows[0]["id"])

    capsys.readouterr()
    env = _run_cli(
        "--db", str(db), "--json", "transcribe-source-item",
        "--source-item-id", str(sid), "--provider", "mock",
        capsys=capsys,
    )
    _assert_envelope(env, subcommand="transcribe-source-item")
    assert set(env["data"].keys()) >= {"source_item_id", "created", "evidence", "error", "provider"}
    assert env["data"]["provider"] == "mock-transcription"
    assert env["data"]["evidence"] is not None
    assert env["data"]["evidence"]["evidence_type"] == "transcript"


def test_ocr_source_item_mock_envelope(tmp_path: Path, capsys) -> None:
    fixture = _FIXTURE_DIR / "whatsapp_wacli_basic.json"
    db = tmp_path / "atlas.db"
    main(["--db", str(db), "init-db"])
    main(["--db", str(db), "import-whatsapp", "--input", str(fixture)])
    from memorymaster.service import MemoryService
    svc = MemoryService(db, workspace_root=tmp_path)
    with svc.store.connect() as conn:
        rows = conn.execute("SELECT id FROM source_items WHERE item_type='image' LIMIT 1").fetchall()
        sid = int(rows[0]["id"])

    capsys.readouterr()
    env = _run_cli(
        "--db", str(db), "--json", "ocr-source-item",
        "--source-item-id", str(sid), "--provider", "mock",
        capsys=capsys,
    )
    _assert_envelope(env, subcommand="ocr-source-item")
    assert env["data"]["provider"] == "mock-ocr"
    assert env["data"]["evidence"]["evidence_type"] == "ocr"


def test_transcribe_failure_records_event_does_not_crash(tmp_path: Path) -> None:
    """Provider failure must be recorded as media_process event, not raised."""
    from memorymaster.bridges.media_processing import process_transcription
    from memorymaster.service import MemoryService

    fixture = _FIXTURE_DIR / "whatsapp_wacli_basic.json"
    db = tmp_path / "atlas.db"
    main(["--db", str(db), "init-db"])
    main(["--db", str(db), "import-whatsapp", "--input", str(fixture)])
    svc = MemoryService(db, workspace_root=tmp_path)
    with svc.store.connect() as conn:
        rows = conn.execute("SELECT id FROM source_items WHERE item_type='audio' LIMIT 1").fetchall()
        sid = int(rows[0]["id"])

    class _AlwaysFails:
        provider_name = "test-failure"
        def transcribe(self, path):  # noqa: ARG002
            raise RuntimeError("simulated provider failure")

    n_before = len(svc.list_events(event_type="media_process"))
    outcome = process_transcription(svc, sid, _AlwaysFails())
    assert outcome.evidence is None
    assert "simulated provider failure" in (outcome.error or "")
    n_after = len(svc.list_events(event_type="media_process"))
    assert n_after > n_before, "failure must be recorded as media_process event"
    failure_events = [e for e in svc.list_events(event_type="media_process") if e.details == "media_process_failed"]
    assert failure_events, "expected a media_process_failed event"


def test_whatsapp_fixture_idempotent_reimport(tmp_path: Path, capsys) -> None:
    """Re-importing the same fixture twice must not duplicate rows."""
    fixture = _FIXTURE_DIR / "whatsapp_wacli_basic.json"
    db = tmp_path / "atlas.db"
    main(["--db", str(db), "init-db"])

    capsys.readouterr()
    env1 = _run_cli(
        "--db", str(db), "--json", "import-whatsapp", "--input", str(fixture),
        capsys=capsys,
    )
    capsys.readouterr()
    env2 = _run_cli(
        "--db", str(db), "--json", "import-whatsapp", "--input", str(fixture),
        capsys=capsys,
    )
    # Second import sees the same items but updates rather than inserts
    assert env2["data"]["source_items_imported"] == 0
    assert env2["data"]["source_items_updated"] == env1["data"]["source_items_imported"]
