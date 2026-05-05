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

from memorymaster.atlas_contract import (
    ATLAS_CONTRACT_NAME,
    ATLAS_CONTRACT_VERSION,
    ATLAS_ENDPOINTS,
    ATLAS_SUBCOMMANDS,
    atlas_contract_payload,
    atlas_meta,
)
from memorymaster.cli import main


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
    from memorymaster.cli import main as cli_main
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
    from memorymaster.cli import main as cli_main
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
    from memorymaster.cli import main as cli_main
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
    from memorymaster.cli import main as cli_main
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
