from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_incident_drill.py"


def _load_drill_module():
    spec = importlib.util.spec_from_file_location("incident_drill_runner_test_module", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load scripts/run_incident_drill.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module.__name__] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _tmp_dir(prefix: str) -> Path:
    base = REPO_ROOT / ".tmp_pytest"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=str(base)))


def test_build_command_plan_contains_expected_commands() -> None:
    mod = _load_drill_module()
    case_dir = _tmp_dir("incident-drill-plan")
    plan = mod.build_command_plan(
        python_executable=sys.executable,
        workspace_root=REPO_ROOT,
        drill_dir=case_dir,
        perf_slo_config="benchmarks/slo_targets.json",
        perf_claims=7,
        perf_queries=5,
        perf_cycles=1,
        include_operator_e2e=True,
        eval_strict=True,
    )

    names = [item.name for item in plan]
    assert names == ["perf_smoke", "eval_memorymaster", "operator_e2e"]

    perf_cmd = plan[0].argv
    assert perf_cmd[1] == "benchmarks/perf_smoke.py"
    assert "--slo-config" in perf_cmd
    assert "benchmarks/slo_targets.json" in perf_cmd
    assert "--claims" in perf_cmd and "7" in perf_cmd

    eval_cmd = plan[1].argv
    assert eval_cmd[1] == "scripts/eval_memorymaster.py"
    assert "--strict" in eval_cmd


def test_should_apply_fix_respects_summary_and_skip_flag() -> None:
    mod = _load_drill_module()
    before = {"summary": {"orphan_events": 0, "orphan_citations": 1, "hash_chain_issues": 0}}

    assert mod.should_apply_fix(before, False) is True
    assert mod.should_apply_fix(before, True) is False
    assert mod.should_apply_fix({"summary": {"orphan_events": 0, "orphan_citations": 0, "hash_chain_issues": 0}}, False) is False


def test_dry_run_writes_plan_artifact() -> None:
    case_dir = _tmp_dir("incident-drill-dry-run")
    artifacts_root = case_dir / "artifacts"
    drill_id = "drill-test"
    cmd = [
        sys.executable,
        str(SCRIPT_PATH),
        "--dry-run",
        "--drill-id",
        drill_id,
        "--artifacts-root",
        str(artifacts_root),
        "--skip-operator-e2e",
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr

    payload = json.loads(proc.stdout)
    assert payload["status"] == "dry_run"
    assert payload["drill_id"] == drill_id
    names = [item["name"] for item in payload["command_plan"]]
    assert names[:2] == ["perf_smoke", "eval_memorymaster"]
    assert "operator_e2e" not in names
    assert len(payload["command_plan"]) >= 2

    run_json = artifacts_root / drill_id / "incident_drill_run.json"
    assert run_json.exists()
    persisted = json.loads(run_json.read_text(encoding="utf-8"))
    assert persisted["status"] == "dry_run"
    assert persisted["command_plan"][0]["name"] == "perf_smoke"
