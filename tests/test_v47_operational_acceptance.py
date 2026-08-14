from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_v47_operational_acceptance.py"


def _module():
    spec = importlib.util.spec_from_file_location("v47_acceptance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _config(module, tmp_path: Path, *, samples: int = 3):
    return module.GateConfig(
        db=tmp_path / "memory.db",
        runtime_python=Path("python"),
        base_url="http://127.0.0.1:8765",
        receipt_file=tmp_path / "receipts.jsonl",
        session_hook=tmp_path / "hook.py",
        retrieval_samples=samples,
    )


def test_exit_codes_are_fail_closed() -> None:
    module = _module()
    passed = module.CheckResult("pass", module.Verdict.PASS, "ok")
    pending = module.CheckResult("pending", module.Verdict.NOT_YET_DUE, "waiting")
    failed = module.CheckResult("failed", module.Verdict.FAIL, "broken")

    assert module.exit_code([passed]) == 0
    assert module.exit_code([passed, pending]) == 3
    assert module.exit_code([passed, pending, failed]) == 1


def test_gate_binds_imports_to_its_repository() -> None:
    module = _module()

    assert Path(module.sys.path[0]).resolve() == module.REPO_ROOT


def test_checkpoint_is_pending_until_due_then_fails_without_receipt() -> None:
    module = _module()
    state = {
        "enabled": True,
        "last_run": "1999-11-30T00:00:00-03:00",
        "next_run": "2026-08-14T11:25:00-03:00",
    }
    before = datetime(2026, 8, 14, 14, 0, tzinfo=timezone.utc)
    after = datetime(2026, 8, 14, 15, 0, tzinfo=timezone.utc)

    pending = module.checkpoint_result("MemoryMaster-Checkpoint-Daily", state, [], before)
    failed = module.checkpoint_result("MemoryMaster-Checkpoint-Daily", state, [], after)

    assert pending.verdict is module.Verdict.NOT_YET_DUE
    assert pending.due_at == "2026-08-14T14:25:00+00:00"
    assert failed.verdict is module.Verdict.FAIL


def test_checkpoint_pass_requires_real_work_receipt() -> None:
    module = _module()
    state = {
        "enabled": True,
        "last_run": "2026-08-14T11:25:02-03:00",
        "next_run": "2026-08-15T11:25:00-03:00",
    }
    transport_only = {
        "task": "MemoryMaster-Checkpoint-Daily",
        "work_performed": False,
        "result": "pass",
        "completed_at": "2026-08-14T14:30:00Z",
    }
    real_work = {**transport_only, "work_performed": True}
    now = datetime(2026, 8, 14, 15, 0, tzinfo=timezone.utc)

    assert module.checkpoint_result(state=state, task_name=transport_only["task"], receipts=[transport_only], now=now).verdict is module.Verdict.FAIL
    assert module.checkpoint_result(state=state, task_name=real_work["task"], receipts=[real_work], now=now).verdict is module.Verdict.PASS


def test_graph_gate_rejects_unknown_support_sensitivity(tmp_path: Path) -> None:
    module = _module()
    db = tmp_path / "memory.db"
    connection = sqlite3.connect(db)
    connection.executescript("""
        CREATE TABLE graph_observation_jobs(stage TEXT, status TEXT);
        INSERT INTO graph_observation_jobs VALUES ('discover', 'completed');
        CREATE TABLE graph_observations(observation_claim_id INTEGER);
        CREATE TABLE graph_observation_supports(
            observation_claim_id INTEGER, supporting_claim_id INTEGER,
            evidence_item_id INTEGER, source_item_id INTEGER,
            source_entity_id INTEGER, target_entity_id INTEGER, relation TEXT
        );
        CREATE TABLE claims(
            id INTEGER PRIMARY KEY, status TEXT, visibility TEXT, claim_type TEXT,
            source_agent TEXT, scope TEXT, tenant_id TEXT, confidence REAL
        );
        INSERT INTO claims VALUES (1, 'confirmed', 'public', 'fact', 'fixture', 'project:test', NULL, 0.9);
        CREATE TABLE entity_edge_supports(
            supporting_claim_id INTEGER, source_entity_id INTEGER, relation TEXT,
            target_entity_id INTEGER, ontology_version TEXT, scope TEXT
        );
        INSERT INTO entity_edge_supports VALUES (1, 10, 'depends_on', 20, 'personal-v1', 'project:test');
        CREATE TABLE claim_evidence_links(claim_id INTEGER, evidence_item_id INTEGER);
        INSERT INTO claim_evidence_links VALUES (1, 1);
        CREATE TABLE evidence_items(id INTEGER PRIMARY KEY, source_item_id INTEGER, sensitivity TEXT);
        INSERT INTO evidence_items VALUES (1, 1, NULL);
        CREATE TABLE source_items(
            id INTEGER PRIMARY KEY, retired_at TEXT, sensitivity TEXT, occurred_at TEXT
        );
        INSERT INTO source_items VALUES (1, NULL, NULL, '2026-08-14T00:00:00Z');
        CREATE TABLE entity_edges(source_id INTEGER, target_id INTEGER, relation TEXT);
    """)
    connection.commit()
    connection.close()

    result = module.check_graph_observations(_config(module, tmp_path))

    assert result.verdict is module.Verdict.FAIL
    assert "unknown_sensitivity_rows=1" in result.detail


def test_retrieval_condition_requires_every_hit_and_latency_budget(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    config = _config(module, tmp_path)
    times = iter((0.0, 0.5, 1.0, 1.4, 2.0, 2.6))
    monkeypatch.setattr(module.time, "perf_counter", lambda: next(times))

    passed = module.check_retrieval(config, retrieve=lambda *_: ["mm-8aef"])

    assert passed.verdict is module.Verdict.PASS
    assert "hits=3/3" in passed.detail
    assert "p95_seconds=0.600" in passed.detail


def test_retrieval_condition_fails_when_target_missing(tmp_path: Path) -> None:
    module = _module()
    config = _config(module, tmp_path, samples=1)

    failed = module.check_retrieval(config, retrieve=lambda *_: ["mm-other"])

    assert failed.verdict is module.Verdict.FAIL
    assert "hits=0/1" in failed.detail
