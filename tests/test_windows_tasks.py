"""The Windows scheduled-task installer: rendering, plan, drift detection and idempotent apply.

Nothing here registers a task or touches the registry: schtasks, HKCU and pip sit behind a fake host and
every path lives under a tmp HOME.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import pytest

from memorymaster.surfaces import setup_hooks, windows_tasks as wt

REPO = Path(__file__).resolve().parents[1]
WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="paths and tasks are Windows-specific")
HERMES = "203.0.113.7"


@pytest.fixture
def manifest():
    return wt.load_manifest(REPO)


def make_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    home = tmp_path / "home"
    env = {
        "USERPROFILE": str(home), "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "USERNAME": "tester", "USERDOMAIN": "BOX", f"{wt.ENV_PREFIX}HERMES_HOST": HERMES,
    }
    env.update(extra)
    return env


class FakeHost:
    """Stands in for schtasks, HKCU and pip; records every mutation."""

    def __init__(self, repo_version: str) -> None:
        self.tasks: dict[str, str] = {}
        self.registry: dict[tuple[str, str], str] = {}
        self.pins: dict[str, dict[str, str]] = {}
        self.versions: dict[str, str] = {}
        self.registered: list[str] = []
        self.commands: list[list[str]] = []
        self.repo_version = repo_version

    def query_task_xml(self, name):
        return self.tasks.get(name)

    def register_task(self, name, xml):
        self.registered.append(name)
        self.tasks[name] = xml

    def registry_get(self, key, name):
        return self.registry.get((key, name))

    def registry_set(self, key, name, value):
        self.registry[(key, name)] = value

    def freeze(self, python):
        return dict(self.pins.get(python, {}))

    def package_version(self, python, package):
        return self.versions.get(python)

    def build_wheel(self, python, repo, out_dir):
        wheel = out_dir / "memorymaster-0-py3-none-any.whl"
        wheel.write_bytes(b"wheel")
        return wheel

    def run(self, argv):
        import subprocess

        self.commands.append(argv)
        if argv[1:3] == ["-m", "venv"]:
            python = Path(argv[3]) / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
        elif "-r" in argv:
            self.pins[argv[0]] = wt.parse_requirements(Path(argv[argv.index("-r") + 1]).read_text(encoding="utf-8"))
        elif "--force-reinstall" in argv:
            self.versions[argv[0]] = self.repo_version
        return subprocess.CompletedProcess(argv, 0, "", "")


def fresh_pc(tmp_path: Path, **env_extra: str):
    env = make_env(tmp_path, **env_extra)
    client = Path(env["LOCALAPPDATA"]) / "Programs" / "Python" / "Python312" / "python.exe"
    client.parent.mkdir(parents=True)
    client.write_bytes(b"")
    return env, FakeHost(wt.repo_version(REPO))


def run_cli(args, host, env):
    lines: list[str] = []
    code = wt.main(args, host=host, env=env, out=lines.append)
    return code, "\n".join(lines)


# --------------------------------------------------------------------------- rendering

def test_every_task_renders_to_wellformed_xml_without_leftover_tokens(manifest, tmp_path):
    values, declared = wt.build_vars(manifest, REPO, make_env(tmp_path), {})
    assert len(manifest["tasks"]) == 12
    for task in manifest["tasks"]:
        xml = wt.task_xml(manifest, task, values, declared)
        assert not re.search(r"\{[A-Z][A-Z0-9_]*\}", xml), task["name"]
        wanted = wt.normalize_xml(xml)
        assert wanted["command"] and wanted["triggers"], task["name"]
    hermes = next(t for t in manifest["tasks"] if t["name"] == "MemoryMaster-MCP-HTTP-Hermes")
    assert f"--host {HERMES} " in wt.task_xml(manifest, hermes, values, declared)


def test_static_wrappers_are_not_changed_by_rendering(manifest, tmp_path):
    """f-strings and PowerShell braces in the shipped wrappers must survive the token pass."""
    values, declared = wt.build_vars(manifest, REPO, make_env(tmp_path), {})
    text = (REPO / "integrations/shared-mcp/windows/memorymaster-mcp-shared.pyw").read_text(encoding="utf-8")
    assert "{PORT}" in text and wt.render(text, values, declared) == text


def test_required_variable_missing_is_a_clear_error(manifest, tmp_path):
    env = make_env(tmp_path)
    env.pop(f"{wt.ENV_PREFIX}HERMES_HOST")
    values, declared = wt.build_vars(manifest, REPO, env, {})
    hermes = next(t for t in manifest["tasks"] if t["name"] == "MemoryMaster-MCP-HTTP-Hermes")
    with pytest.raises(wt.WindowsTasksError, match="HERMES_HOST"):
        wt.task_xml(manifest, hermes, values, declared)


def test_manifest_and_new_files_carry_no_personal_values():
    forbidden = re.compile(r"pauol|WOLVERIN0|192\.168\.|S-1-5-21|Bearer |token\s*=\s*['\"][A-Za-z0-9_-]{16,}", re.I)
    paths = [REPO / "ops/windows-tasks/manifest.json", REPO / "memorymaster/surfaces/windows_tasks.py",
             *sorted((REPO / "ops/windows-tasks/wrappers").iterdir()), *sorted((REPO / "ops/windows-tasks/requirements").iterdir())]
    for path in paths:
        assert not forbidden.search(path.read_text(encoding="utf-8", errors="replace")), path.name


def test_redaction_replaces_machine_values_by_tokens(manifest, tmp_path):
    values, _ = wt.build_vars(manifest, REPO, make_env(tmp_path), {})
    redact = wt.make_redactor(values, True)
    shown = redact(f"{values['HOME']}\\x {HERMES}:8765 {values['REPO']}")
    assert HERMES not in shown and values["HOME"] not in shown and "{HOME}" in shown and "{HERMES_HOST}" in shown


# --------------------------------------------------------------------------- drift normalisation

LIVE_BEAT = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><URI>\\MemoryMaster-Beat</URI></RegistrationInfo>
  <Principals><Principal id="Author"><UserId>S-1-5-21-1-2-3-1001</UserId><LogonType>InteractiveToken</LogonType></Principal></Principals>
  <Settings>
    <DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT20M</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <IdleSettings><Duration>PT10M</Duration></IdleSettings>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
  </Settings>
  <Triggers><TimeTrigger><StartBoundary>2020-05-05T11:40:35-03:00</StartBoundary>
    <Repetition><Interval>PT5M</Interval><Duration>P3650D</Duration><StopAtDurationEnd>true</StopAtDurationEnd></Repetition>
  </TimeTrigger></Triggers>
  <Actions Context="Author"><Exec><Command>C:\\rt\\Scripts\\pythonw.exe</Command><Arguments>ARGS</Arguments></Exec></Actions>
</Task>"""


def beat_task(manifest, tmp_path, runtime_root="C:\\rt"):
    env = make_env(tmp_path, **{f"{wt.ENV_PREFIX}RUNTIME": runtime_root})
    values, declared = wt.build_vars(manifest, REPO, env, {})
    task = next(t for t in manifest["tasks"] if t["name"] == "MemoryMaster-Beat")
    task = copy.deepcopy(task)
    task["action"]["command"] = "C:\\rt\\Scripts\\pythonw.exe"
    return task, values, declared


def test_live_sid_dates_and_idle_settings_do_not_count_as_drift(manifest, tmp_path):
    task, values, declared = beat_task(manifest, tmp_path)
    xml = wt.task_xml(manifest, task, values, declared)
    args = wt.normalize_xml(xml)["arguments"]
    live = wt.normalize_xml(LIVE_BEAT.replace("ARGS", args.replace("&", "&amp;")))
    assert wt.diff_normalized(live, wt.normalize_xml(xml)) == []


def test_a_different_provider_in_the_live_task_is_reported(manifest, tmp_path):
    task, values, declared = beat_task(manifest, tmp_path)
    xml = wt.task_xml(manifest, task, values, declared)
    args = wt.normalize_xml(xml)["arguments"].replace("--extract-model haiku", "--extract-model gemini-flash")
    live = wt.normalize_xml(LIVE_BEAT.replace("ARGS", args))
    drift = wt.diff_normalized(live, wt.normalize_xml(xml))
    assert len(drift) == 1 and drift[0].startswith("arguments:") and "gemini-flash" in drift[0]


def test_changed_trigger_and_setting_are_reported(manifest, tmp_path):
    task, values, declared = beat_task(manifest, tmp_path)
    xml = wt.task_xml(manifest, task, values, declared)
    args = wt.normalize_xml(xml)["arguments"]
    live_text = LIVE_BEAT.replace("ARGS", args).replace("PT5M", "PT10M").replace("PT20M", "PT1H")
    drift = wt.diff_normalized(wt.normalize_xml(live_text), wt.normalize_xml(xml))
    assert any("repetition" in line for line in drift) and any("execution_time_limit" in line for line in drift)


# --------------------------------------------------------------------------- plan / check / apply

@WINDOWS_ONLY
def test_plan_is_the_default_and_writes_nothing(tmp_path):
    env, host = fresh_pc(tmp_path)
    code, output = run_cli([], host, env)
    assert code == 0
    assert "MemoryMaster-Beat" in output and "[create" in output
    assert host.registered == [] and host.commands == [] and host.registry == {}
    assert not (Path(env["USERPROFILE"]) / ".memorymaster").exists()
    assert HERMES not in output and "tester" not in output  # redacted


@WINDOWS_ONLY
def test_apply_without_a_secret_stops_before_any_change(tmp_path):
    env, host = fresh_pc(tmp_path)
    code, _ = run_cli(["--apply"], host, env)
    assert code == 2
    assert host.registered == [] and host.commands == [] and host.registry == {}
    assert not (Path(env["USERPROFILE"]) / ".memorymaster").exists()


@WINDOWS_ONLY
def test_apply_is_idempotent_and_check_is_clean_afterwards(tmp_path):
    env, host = fresh_pc(tmp_path, MEMORYMASTER_MCP_HTTP_TOKEN="shared-secret-value",
                         MEMORYMASTER_HERMES_MCP_HTTP_TOKEN="hermes-secret-value")
    (tmp_path / "w.whl").write_bytes(b"x")
    code, output = run_cli(["--apply", "--wheel", str(tmp_path / "w.whl")], host, env)
    assert code == 0, output
    assert "shared-secret-value" not in output and "hermes-secret-value" not in output
    names = {t["name"] for t in wt.load_manifest(REPO)["tasks"]}
    blocked = {"MemoryMasterWeeklyDigest"} if not (REPO / "scripts/atlas_weekly_digest.py").exists() else set()
    assert set(host.registered) == names - blocked
    assert host.registry[("Software\\MemoryMaster\\SharedMcp", "MEMORYMASTER_MCP_HTTP_TOKEN")] == "shared-secret-value"
    assert host.registry[("Software\\MemoryMaster\\HermesMcp", "MEMORYMASTER_MCP_HTTP_TOKEN")] == "hermes-secret-value"
    assert host.registry[("Environment", "MEMORYMASTER_BEAT_ENABLED")] == "1"
    home = Path(env["USERPROFILE"])
    assert (home / ".memorymaster/runtime/graph-profile-20260813/run-memorymaster-dreaming.pyw").is_file()
    assert (home / ".claude/hooks/memorymaster-steward-cycle.py").is_file()
    config = json.loads((Path(env["LOCALAPPDATA"]) / "MemoryMaster/operational-review/config.json").read_text(encoding="utf-8"))
    assert config["db"].endswith("memorymaster.db") and "expected_version" not in config

    registered, commands = list(host.registered), len(host.commands)
    code, output = run_cli(["--apply", "--wheel", str(tmp_path / "w.whl")], host, env)
    assert code == 0 and host.registered == registered and len(host.commands) == commands
    assert "[create" not in output and "[update" not in output

    code, output = run_cli(["--check"], host, env)
    drift_lines = [ln for ln in output.splitlines() if ln.startswith("[") and not ln.startswith("[unchanged")]
    # only what the repo cannot supply remains: the removed digest script and the infra repo's backup scripts
    assert all(ln.startswith(("[blocked", "[external-missing", "[optional-missing")) for ln in drift_lines), drift_lines
    assert code == (1 if drift_lines else 0)


@WINDOWS_ONLY
def test_apply_backs_up_a_replaced_task_and_keeps_operator_owned_files(tmp_path):
    env, host = fresh_pc(tmp_path, MEMORYMASTER_MCP_HTTP_TOKEN="a", MEMORYMASTER_HERMES_MCP_HTTP_TOKEN="b")
    (tmp_path / "w.whl").write_bytes(b"x")
    wheel = ["--wheel", str(tmp_path / "w.whl")]
    run_cli(["--apply", *wheel], host, env)
    host.tasks["MemoryMaster-Beat"] = host.tasks["MemoryMaster-Beat"].replace("PT5M", "PT9M")
    steward = Path(env["USERPROFILE"]) / ".claude/hooks/memorymaster-steward-cycle.py"
    steward.write_text("operator edit", encoding="utf-8")
    host.registry[("Environment", "MEMORYMASTER_BEAT_ENABLED")] = "0"

    code, check = run_cli(["--check"], host, env)
    assert code == 1 and "[update" in check and "[differs-kept" in check

    host.registered.clear()
    run_cli(["--apply", *wheel], host, env)
    assert host.registered == ["MemoryMaster-Beat"]
    backups = list((Path(env["USERPROFILE"]) / ".memorymaster/runtime/task-backups").glob("MemoryMaster-Beat.*.xml"))
    assert len(backups) == 1 and "PT9M" in backups[0].read_text(encoding="utf-8")
    assert steward.read_text(encoding="utf-8") == "operator edit"
    assert host.registry[("Environment", "MEMORYMASTER_BEAT_ENABLED")] == "0"


@WINDOWS_ONLY
def test_task_with_a_missing_repo_file_is_blocked_not_registered(manifest, tmp_path):
    env, host = fresh_pc(tmp_path)
    values, declared = wt.build_vars(manifest, REPO, env, {})
    broken = copy.deepcopy(manifest)
    broken["tasks"] = [t for t in broken["tasks"] if t["name"] == "MemoryMaster-Beat"]
    broken["tasks"][0]["requires_repo_files"] = ["does/not/exist.py"]
    items = wt.Reconciler(REPO, broken, values, declared, host, env, [], apply=False).run()
    assert [i["status"] for i in items if i["kind"] == "task"] == [wt.BLOCKED]


def test_setup_surface_routes_the_windows_tasks_subcommand(monkeypatch):
    seen = []
    monkeypatch.setattr(wt, "main", lambda argv=None, **kw: seen.append(argv) or 0)
    assert setup_hooks.main(["windows-tasks", "--check"]) == 0
    assert seen == [["--check"]]
