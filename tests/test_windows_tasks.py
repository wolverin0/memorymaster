"""The Windows scheduled-task installer: rendering, plan, drift detection and idempotent apply.

Nothing here registers a task or touches the registry: schtasks, HKCU and pip sit behind a fake host and
every path lives under a tmp HOME.\n"""

from __future__ import annotations

import copy
import json
import os
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
    assert len(manifest["tasks"]) == 15
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
    # Machine identity is read from whoever runs the test, so no username or host name is written down here.
    own = {os.environ.get(name, "") for name in ("USERNAME", "USER", "USERDOMAIN", "COMPUTERNAME")}
    own.add(Path.home().name)
    own = sorted(re.escape(value) for value in own if len(value) >= 4)
    generic = [r"C:\\Users\\[^\\\s\"]+", r"192\.168\.", r"S-1-5-21", r"Bearer ", r"token\s*=\s*['\"][A-Za-z0-9_-]{16,}"]
    forbidden = re.compile("|".join(own + generic), re.I)
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
    # only what the repo cannot supply remains: the infra repo's backup scripts
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
    assert len(backups) == 1
    raw = backups[0].read_bytes()
    # The header declares UTF-16, so the bytes must be UTF-16 with a BOM (schtasks /Create /XML restores it).
    assert raw[:2] in (b"\xff\xfe", b"\xfe\xff")
    text = raw.decode("utf-16")
    assert 'encoding="UTF-16"' in text and "PT9M" in text
    assert wt.normalize_xml(text)["triggers"][0]["repetition"]["interval"] == "PT9M"
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


# --------------------------------------------------------------------------- T-2077a review findings

def test_user_env_never_pairs_the_claude_cli_with_a_gemini_model(manifest, monkeypatch):
    """M5: DREAM_EXTRACT_PROVIDER=claude plus a Gemini model made capture run `claude --model gemini-...`."""
    from memorymaster.capture.worker import capture_llm_overrides

    for name in ("MEMORYMASTER_CAPTURE_LLM_PROVIDER", "MEMORYMASTER_LLM_PROVIDER",
                 "MEMORYMASTER_CAPTURE_LLM_MODEL", "MEMORYMASTER_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in manifest["user_env"].items():
        monkeypatch.setenv(name, value)
    overrides = capture_llm_overrides()
    assert overrides.get("MEMORYMASTER_LLM_PROVIDER") == "claude_cli"
    assert "gemini" not in overrides.get("MEMORYMASTER_LLM_MODEL", "").lower()


def test_manifest_user_env_does_not_set_a_gemini_model_for_the_claude_provider(manifest):
    env = manifest["user_env"]
    if env.get("MEMORYMASTER_DREAM_EXTRACT_PROVIDER") == "claude":
        assert "gemini" not in env.get("MEMORYMASTER_DREAM_EXTRACT_MODEL", "").lower()


def test_capture_ignores_a_gemini_dream_model_when_the_provider_is_the_claude_cli(monkeypatch):
    """M5, live shape: the registry already holds claude + a Gemini model; the code must not forward it."""
    from memorymaster.capture.worker import capture_llm_overrides

    for name in ("MEMORYMASTER_CAPTURE_LLM_PROVIDER", "MEMORYMASTER_LLM_PROVIDER",
                 "MEMORYMASTER_CAPTURE_LLM_MODEL", "MEMORYMASTER_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMORYMASTER_DREAM_EXTRACT_PROVIDER", "claude")
    monkeypatch.setenv("MEMORYMASTER_DREAM_EXTRACT_MODEL", "gemini-3.5-flash-lite")
    overrides = capture_llm_overrides()
    assert overrides["MEMORYMASTER_LLM_PROVIDER"] == "claude_cli"
    assert "MEMORYMASTER_LLM_MODEL" not in overrides
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_LLM_MODEL", "sonnet")  # an explicit capture model still wins
    assert capture_llm_overrides()["MEMORYMASTER_LLM_MODEL"] == "sonnet"


@WINDOWS_ONLY
def test_an_explicit_wheel_hash_is_checked_even_when_the_installed_version_matches(tmp_path, capsys):
    """M6: same installed version used to skip the wheel entirely, so a bad --wheel-sha256 went unnoticed."""
    env, host = fresh_pc(tmp_path)
    python = Path(env["LOCALAPPDATA"]) / "Programs" / "Python" / "Python312" / "python.exe"
    host.versions[str(python)] = wt.repo_version(REPO)
    wheel = tmp_path / "w.whl"
    wheel.write_bytes(b"x")
    for mode in ([], ["--apply"]):
        code, output = run_cli([*mode, "--wheel", str(wheel), "--wheel-sha256", "0" * 64], host, env)
        assert code == 2 and "hash mismatch" in output + capsys.readouterr().err, (mode, output)
    assert host.registered == []


def test_a_default_that_embeds_a_required_variable_does_not_leak_the_token(manifest, tmp_path):
    """L25: a default built from a required variable that has no value must raise, not render literally."""
    broken = copy.deepcopy(manifest)
    broken["variables"]["defaults"]["RELAY"] = "http://{HERMES_HOST}:8788"
    task = copy.deepcopy(next(t for t in broken["tasks"] if t["name"] == "MemoryMaster-Beat"))
    task["action"]["arguments"] += " --relay {RELAY}"
    env = make_env(tmp_path)
    env.pop(f"{wt.ENV_PREFIX}HERMES_HOST")
    values, declared = wt.build_vars(broken, REPO, env, {})
    with pytest.raises(wt.WindowsTasksError, match="HERMES_HOST"):
        wt.task_xml(broken, task, values, declared)
    values, declared = wt.build_vars(broken, REPO, make_env(tmp_path), {})
    assert f"--relay http://{HERMES}:8788" in wt.task_xml(broken, task, values, declared)


def test_a_relative_repo_is_resolved_to_an_absolute_path(monkeypatch):
    """L26: find_repo(--repo .) baked a relative path into registered tasks."""
    monkeypatch.chdir(REPO.parent)
    found = wt.find_repo(REPO.name)
    assert found.is_absolute() and found == REPO


def test_the_digest_cmdline_argument_is_quoted(manifest, tmp_path):
    """L24: every other path is quoted; a HOME with spaces split this one."""
    values, declared = wt.build_vars(manifest, REPO, make_env(tmp_path), {})
    digest = next(t for t in manifest["tasks"] if t["name"] == "MemoryMasterWeeklyDigest")
    xml = wt.task_xml(manifest, digest, values, declared)
    assert re.search(r'MemoryMasterWeeklyDigest\.cmdline(&quot;|")</Arguments>', xml), xml


_STUBS = {
    "__init__.py": "",
    "service.py": (
        "class MemoryService:\n"
        "    def __init__(self, *a, **k):\n"
        "        if {fail!r} == 'cycle_init':\n"
        "            raise RuntimeError('stub failure: cycle_init')\n"
        "        self.store = object()\n"
        "    def run_cycle(self, **k):\n"
        "        return {{}}\n"
    ),
    "govern/__init__.py": "",
    "govern/jobs/__init__.py": "",
    "govern/jobs/revalidation.py": (
        "def tenants_with_work(store):\n"
        "    if {fail!r} == 'revalidation':\n"
        "        raise RuntimeError('stub failure: revalidation')\n"
        "    return []\n"
    ),
    "govern/candidate_dedupe.py": "def run_jev(store, limit):\n    return {{}}\ndef jev_pairs_per_cycle():\n    return 1\n",
    "govern/fuzzy_dedupe.py": "def run(store):\n    return {{}}\n",
    "govern/jobs/scheduled_archive.py": "def run(svc, older_than_days):\n    return {{'archived': 0}}\n",
    "govern/jobs/curation_drain.py": "def run(svc, limit, apply):\n    return {{}}\n",
    "core/__init__.py": "",
    "core/llm_provider.py": "def purge_claude_cli_scratch():\n    return {{}}\n",
    "decisions/__init__.py": "",
    "decisions/outcomes.py": "def steward_cycle_outcomes(db):\n    return {{}}\n",
    "wiki_engine.py": (
        "def absorb(db, path):\n"
        "    if {fail!r} == 'wiki_absorb':\n"
        "        raise RuntimeError('stub failure: wiki_absorb')\n"
        "    return {{}}\n"
        "def cleanup(path, scope_filter=None):\n"
        "    return {{}}\n"
        "def breakdown(db, path, scope_filter=None):\n"
        "    return {{}}\n"
    ),
}


@pytest.mark.parametrize("fail,exit_ok", [
    (None, True),  # every stage works: still exit 0
    ("cycle_init", False),
    ("revalidation", False),
    ("wiki_absorb", False),
])
def test_the_steward_wrapper_exit_code_reflects_every_stage_failure(tmp_path, fail, exit_ok):
    """L23: a stage failure was printed to stderr and the scheduled task still ended with exit 0."""
    import subprocess

    package = tmp_path / "stub" / "memorymaster"
    for rel, body in _STUBS.items():
        target = package / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body.format(fail=fail), encoding="utf-8")
    root = tmp_path / "repo"
    (root / "obsidian-vault" / "wiki").mkdir(parents=True)
    source = (REPO / "ops/windows-tasks/wrappers/steward-cycle.py").read_text(encoding="utf-8")
    script = tmp_path / "steward-cycle.py"
    script.write_text(source.replace("{REPO}", str(root)), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "stub"), "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
           "MEMORYMASTER_WIKI_ABSORB": "1"}
    done = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=60)
    if fail:
        assert f"stub failure: {fail}" in done.stderr, done.stderr
    else:
        assert done.stderr == "", done.stderr
    assert (done.returncode == 0) is exit_ok, done.stdout + done.stderr


@WINDOWS_ONLY
def test_the_wheel_is_hashed_once_and_the_hash_is_case_insensitive(tmp_path, monkeypatch):
    """Review of #356: the wheel was hashed up front and again at install; an uppercase digest must match."""
    import hashlib

    env, host = fresh_pc(tmp_path, MEMORYMASTER_MCP_HTTP_TOKEN="shared-secret-value",
                         MEMORYMASTER_HERMES_MCP_HTTP_TOKEN="hermes-secret-value")
    wheel = tmp_path / "w.whl"
    wheel.write_bytes(b"x")
    calls = []
    real = hashlib.sha256
    monkeypatch.setattr(wt.hashlib, "sha256", lambda *a, **k: calls.append(1) or real(*a, **k))
    digest = real(b"x").hexdigest().upper()
    code, output = run_cli(["--apply", "--wheel", str(wheel), "--wheel-sha256", digest], host, env)
    assert code == 0, output
    assert any("--force-reinstall" in cmd for cmd in host.commands)  # the install path really ran
    assert len(calls) == 1


def test_the_health_probe_task_runs_daily_hidden_with_the_repo_probe(manifest, tmp_path):
    """T-2121: a daily, windowless run of scripts/memory_health_probe.py; no token is left and no apply is needed to see it."""
    task = next(t for t in manifest["tasks"] if t["name"] == "MemoryMaster-Health-Probe")
    values, declared = wt.build_vars(manifest, REPO, make_env(tmp_path), {})
    xml = wt.task_xml(manifest, task, values, declared)
    assert not re.search(r"\{[A-Z][A-Z0-9_]*\}", xml)
    assert task["triggers"] == [{"type": "daily", "at": "09:15:00"}]
    assert "pythonw.exe" in xml and "memory_health_probe.py" in xml and "checkpoints" in xml
    for needed in task["requires_repo_files"]:
        assert (REPO / needed).is_file(), needed


def test_verbatim_archive_runs_weekly_from_the_graph_profile_runtime(manifest):
    """T-2116: pruning is scheduled so the verbatim table cannot grow past retention again."""
    task = next(t for t in manifest["tasks"] if t["name"] == "MemoryMaster-Verbatim-Archive-Weekly")
    assert task["triggers"][0]["type"] == "weekly"
    assert "scheduled_task verbatim-archive" in task["action"]["arguments"]
    assert "graph-profile" in task["runtimes"]


def test_transcript_index_refreshes_hourly_from_the_graph_profile_runtime(manifest):
    task = next(t for t in manifest["tasks"] if t["name"] == "MemoryMaster-Transcript-Index-Hourly")
    assert task["triggers"][0]["repetition"]["interval"] == "PT1H"
    assert "scheduled_task transcript-index" in task["action"]["arguments"]
    assert "graph-profile" in task["runtimes"]


# --------------------------------------------------------------------------- CODE: scripts from one checkout, DB and workspace from another

def test_code_defaults_to_the_checkout_holding_the_manifest(manifest, tmp_path):
    values, declared = wt.build_vars(manifest, REPO, make_env(tmp_path), {})
    assert "CODE" in declared and values["CODE"] == values["REPO"] == str(REPO)


@WINDOWS_ONLY
def test_scripts_run_from_the_code_checkout_while_db_and_workspace_stay_in_the_main_one(manifest, tmp_path):
    """`--repo <live-main> --var REPO=<main>`: every script path is {CODE}, DB and workspace are {REPO}."""
    live, main = Path(r"C:\work\live-main"), r"C:\work\main"
    values, declared = wt.build_vars(manifest, live, make_env(tmp_path), {"REPO": main})
    assert values["CODE"] == str(live) and values["REPO"] == main and values["DB"] == main + r"\memorymaster.db"

    probe_task = next(t for t in manifest["tasks"] if t["name"] == "MemoryMaster-Health-Probe")
    args = wt.task_xml(manifest, probe_task, values, declared)
    assert rf"{live}\scripts\memory_health_probe.py" in args
    assert rf'--db "{main}\memorymaster.db" --workspace "{main}"' in args

    for wrapper, script in (("mm_weekly_digest.cmd", "atlas_weekly_digest.py"), ("run-hermes-sync-hidden.vbs", "windows-hermes-sync.ps1")):
        text = wt.render((REPO / "ops/windows-tasks/wrappers" / wrapper).read_text(encoding="utf-8"), values, declared)
        assert rf"{live}\scripts\{script}" in text and main not in text


@WINDOWS_ONLY
def test_code_can_be_overridden_on_its_own_and_is_redacted_as_its_own_token(manifest, tmp_path):
    values, _ = wt.build_vars(manifest, REPO, make_env(tmp_path), {"CODE": r"C:\elsewhere\live-main"})
    assert values["CODE"] == r"C:\elsewhere\live-main" and values["REPO"] == str(REPO)
    shown = wt.make_redactor(values, True)(rf"{values['CODE']}\scripts\x.py {values['REPO']}\memorymaster.db")
    assert shown.startswith(r"{CODE}\scripts") and "{REPO}" in shown


def test_no_script_path_in_the_manifest_or_its_wrappers_hangs_off_repo(manifest):
    """A script is code (CODE); only the DB, the workspace and the steward's data root are REPO."""
    for task in manifest["tasks"]:
        action = task["action"]
        text = f"{action['command']} {action.get('arguments', '')}"
        assert not re.search(r"\{REPO\}\+scripts", text), task["name"]
    for wrapper in (REPO / "ops/windows-tasks/wrappers").iterdir():
        assert not re.search(r"\{REPO\}\scripts", wrapper.read_text(encoding="utf-8", errors="replace")), wrapper.name
