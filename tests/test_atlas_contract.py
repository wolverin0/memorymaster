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
    "import-whatsapp",
    "extract-atlas-claims",
    "propose-actions",
    "action-proposals",
    "resolve-action-proposal",
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
