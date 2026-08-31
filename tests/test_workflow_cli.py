from __future__ import annotations

import json

from memorymaster.surfaces.cli import main as cli_main
from memorymaster.surfaces.cli import build_parser
from memorymaster.surfaces import setup_hooks


def test_workflow_cli_surface() -> None:
    parser = build_parser()
    scan = parser.parse_args(["workflow", "scan", "--deep", "human"])
    assert scan.command == "workflow"
    assert scan.workflow_command == "scan"
    assert scan.deep == "human"

    selected = parser.parse_args(
        ["--json", "workflow", "scan", "--deep", "selected", "--session", "s1"]
    )
    assert selected.session == ["s1"]

    review = parser.parse_args(
        ["workflow", "review", "candidate-1", "--decision", "watch"]
    )
    assert review.decision == "watch"

    proposal = parser.parse_args(
        ["workflow", "proposal", "candidate-1", "--output", "proposal.json"]
    )
    assert proposal.output == "proposal.json"


def test_workflow_cli_runs_without_opening_main_memory_database(tmp_path, capsys) -> None:
    workflow_db = tmp_path / "workflow.db"
    main_db = tmp_path / "must-not-exist.db"
    rc = cli_main([
        "--db", str(main_db), "--workflow-db", str(workflow_db),
        "--json", "workflow", "shadow-status",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["ready_for_operator_approval"] is False
    assert workflow_db.is_file()
    assert not main_db.exists()


def test_setup_dry_run_does_not_create_project_or_hook_files(tmp_path, capsys) -> None:
    project = tmp_path / "not-created"
    rc = setup_hooks.main([
        "--dry-run", "--project-root", str(project),
        "--workflow-receipts", "shadow", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["dry_run"] is True
    assert payload["workflow_receipts"]["requested"] == "shadow"
    assert not project.exists()
