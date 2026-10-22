from dataclasses import replace
import json
import sqlite3

import pytest

from memorymaster.profile.project_projection import ProjectProjectionConfig
from memorymaster.profile.project_repository import load_project_snapshot, snapshot_since


@pytest.fixture
def project_config(tmp_path):
    root = tmp_path / "demo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    db = tmp_path / "fixture.sqlite"
    with sqlite3.connect(db) as conn:
        conn.executescript("""CREATE TABLE claims(id INTEGER PRIMARY KEY,text TEXT,
            scope TEXT,tenant_id TEXT,status TEXT,visibility TEXT,source_agent TEXT,
            replaced_by_claim_id INTEGER,version INTEGER DEFAULT 1,claim_type TEXT,
            subject TEXT,predicate TEXT,object_value TEXT,pinned INTEGER DEFAULT 0,
            created_at TEXT,updated_at TEXT,event_time TEXT,valid_from TEXT,valid_until TEXT);
            CREATE TABLE citations(id INTEGER PRIMARY KEY,claim_id INTEGER,source TEXT,
            locator TEXT,excerpt TEXT,created_at TEXT);""")
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    (ledger / "tasks.json").write_text(json.dumps({"complete": True, "tasks": []}))
    return ProjectProjectionConfig(
        db_path=db,
        scope="project:demo",
        tenant_id=None,
        context_key="fixture",
        epoch="pilot",
        project_root=root,
        output_root=tmp_path / "out",
        state_root=tmp_path / "state",
        ledger_root=ledger,
        ledger_repo="demo",
        ledger_filtered=True,
        enabled=True,
    )


def claim(config, id=1, text="Use bounded reads", **changes):
    values = dict(
        id=id,
        text=text,
        scope="project:demo",
        tenant_id=None,
        status="confirmed",
        visibility="public",
        source_agent="operator",
        version=1,
        claim_type="decision",
        created_at="2026-10-01T00:00:00+00:00",
        updated_at="2026-10-01T00:00:00+00:00",
    )
    values.update(changes)
    with sqlite3.connect(config.db_path) as conn:
        conn.execute(
            f"INSERT INTO claims ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values())
        )
        conn.execute(
            "INSERT INTO citations(claim_id,source,locator,excerpt) VALUES (?,?,?,?)",
            (id, "session", f"session:{id}", "Authored evidence"),
        )


def test_confirmed_authorized_current_selection(project_config):
    config = project_config
    claim(config)
    for i, changes in enumerate(
        [
            {"status": "candidate"},
            {"scope": "project:other"},
            {"tenant_id": "other"},
            {"visibility": "private"},
            {"valid_until": "2001-01-01"},
            {"valid_from": "2999-01-01"},
            {"replaced_by_claim_id": 1},
            {"source_agent": "project-projection"},
            {"subject": "password=secret-password"},
        ],
        2,
    ):
        claim(config, i, **changes)
    claim(config, 20, subject="engine", predicate="uses", object_value=None)
    result = load_project_snapshot(config)
    assert set(result["items"]) == {"claim:1", "claim:20"}
    assert result["complete"]
    assert result["coverage"]["claim:1"]["kind"] == "claim"


def test_ledger_actual_schema_visibility_and_completeness(project_config):
    config = project_config
    path = config.ledger_root / "tasks.json"
    path.write_text(
        json.dumps(
            {
                "complete": True,
                "tasks": [
                    {"id": "T-1", "repo": "demo", "state": "ready", "title": "Visible"},
                    {"id": "T-2", "repo": "demo", "state": "blocked", "title": "Hidden", "blind": True},
                    {"id": "T-3", "repo": "other", "state": "ready", "title": "Other"},
                    {"id": "T-4", "repo": "demo", "state": "done", "title": "Closed"},
                ],
            }
        )
    )
    result = load_project_snapshot(config)
    assert set(result["items"]) == {"card:T-1", "card:T-4"}
    assert set(result["coverage"]) == {"card:T-1"}
    path.write_text("{")
    result = load_project_snapshot(config)
    assert not result["complete"]
    assert "ledger_unavailable" in result["diagnostics"]


def test_incremental_baseline_rehydrates_coverage_and_changed_rows(project_config):
    config = project_config
    claim(config)
    baseline = load_project_snapshot(config)
    claim(config, 2, status="candidate")
    with sqlite3.connect(config.db_path) as conn:
        conn.execute("UPDATE claims SET status='confirmed',updated_at=? WHERE id=2", (baseline["read_at"],))
        conn.execute("UPDATE citations SET excerpt='Edited evidence' WHERE claim_id=1")
    current = snapshot_since(config, baseline["coverage"], baseline["read_at"])
    assert set(current["coverage"]) == {"claim:1", "claim:2"}
    assert current["coverage"]["claim:1"] != baseline["coverage"]["claim:1"]


def test_explicit_context_and_disabled_have_no_source_reads(project_config):
    config = replace(project_config, enabled=False, db_path=project_config.db_path / "missing")
    assert load_project_snapshot(config)["diagnostics"] == ["inactive"]
    with pytest.raises(ValueError):
        replace(project_config, context_key="../escape")
    with pytest.raises(ValueError):
        replace(project_config, db_path="postgresql://localhost/db")


def test_oversized_citation_cannot_hide_sensitive_or_generated_payload(project_config):
    claim(project_config)
    with sqlite3.connect(project_config.db_path) as conn:
        conn.execute(
            "INSERT INTO citations(claim_id,source,locator,excerpt) VALUES (1,?,?,?)",
            ("session", "extra", "x" * 17000 + "<!-- generated by MemoryMaster -->"),
        )
    snapshot = load_project_snapshot(project_config)
    assert "claim:1" not in snapshot["items"]
    assert not snapshot["complete"]


def test_incremental_includes_temporal_boundary_without_row_edit(project_config):
    claim(project_config, valid_from="2026-10-01T00:00:00+00:00")
    snapshot = snapshot_since(project_config, {}, "2026-09-30T00:00:00+00:00")
    assert "claim:1" in snapshot["coverage"]
    assert "next_temporal_boundary" in snapshot
