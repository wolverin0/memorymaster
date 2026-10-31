"""Fixture contract checks cannot silently claim real CLI delivery."""
import json
import subprocess
import sys

import pytest

from memorymaster.surfaces import hooks_doctor as hd, setup_hooks
from memorymaster.surfaces.cli import main


@pytest.fixture(autouse=True)
def _doctor_scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(hd.tempfile, "tempdir", str(tmp_path))


@pytest.mark.parametrize("cli", ["claude", "codex"])
def test_additional_context_contract(cli):
    output = json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "cited memory"}})
    assert hd.validate_output(cli, "SessionStart", output) == (True, True)
    assert hd.validate_output(cli, "UserPromptSubmit", output) == (False, False)
    assert hd.validate_output(cli, "SessionStart", "debug\n" + output) == ((True, True) if cli == "codex" else (False, False))
    assert hd.validate_output(cli, "SessionStart", '{"continue":"yes"}') == (False, False)


def test_agy_strict_protojson_contract():
    assert hd.validate_output("agy", "SessionStart", '{"injectSteps":[{"ephemeralMessage":"memory"}]}') == (True, True)
    assert hd.validate_output("agy", "SessionStart", '{"hookSpecificOutput":{}}') == (False, False)
    assert hd.validate_output("agy", "SessionStart", '{"injectSteps":[{"ephemeralMessage":12}]}') == (False, False)
    assert hd.validate_output("agy", "Stop", '{"decision":"continue","reason":"done"}') == (True, False)
    assert hd.validate_output("agy", "SessionStart", '{"injectSteps":[{"ephemeralMessage":" "}]}') == (True, False)
    assert hd.validate_output("agy", "SessionStart", '{"injectSteps":[{"systemMessage":{"unknown":12}}]}') == (False, False)


def test_codex_plain_context_contract():
    assert hd.validate_output("codex", "SessionStart", "plain cited memory") == (True, True)
    assert hd.validate_output("codex", "Stop", "plain cited memory") == (False, False)
    assert hd.validate_output("codex", "SessionStart", '{"broken":') == (False, False)


def _install_command(tmp_path, cli, event, body, *, timeout=10):
    folder = tmp_path / cli
    folder.mkdir(exist_ok=True)
    script = folder / "memorymaster-fixture.py"
    script.write_text(body, encoding="utf-8")
    handler = {"type": "command", "command": f'"{sys.executable}" "{script}"', "timeout": timeout}
    if cli == "agy":
        config = {"memorymaster": {"enabled": True, event: [handler]}}
    else:
        config = {"hooks": {event: [{"hooks": [handler]}]}}
    (folder / ("settings.json" if cli == "claude" else "hooks.json")).write_text(json.dumps(config))
    return folder


@pytest.mark.parametrize("cli,event,field", [("claude", "UserPromptSubmit", "prompt"),
                                           ("codex", "SessionStart", "session_id")])
def test_commands_receive_cli_payload_and_output_is_checked(tmp_path, cli, event, field):
    response = ({"injectSteps": [{"ephemeralMessage": "memory"}]} if cli == "agy" else
                {"hookSpecificOutput": {"hookEventName": event, "additionalContext": "memory"}})
    folder = _install_command(tmp_path, cli, event,
                              f"import json,sys; data=json.load(sys.stdin); assert data[{field!r}]; print({json.dumps(response)!r})")
    report = hd.doctor({cli: folder})
    assert report["status"] == "PASS", report
    assert report["checks"][0]["injected"] is True
    assert "unverified" in report["acceptance"]


def test_quiet_capture_cannot_pass_as_automatic_recall(tmp_path):
    folder = _install_command(tmp_path, "codex", "Stop", "")
    report = hd.doctor({"codex": folder})
    assert report["status"] == "WARN"
    assert report["checks"][0]["valid_output"] is True
    assert report["checks"][0]["injected"] is False


@pytest.mark.parametrize("body", ["print('debug: not JSON')", "raise SystemExit(2)"])
def test_bad_output_and_exit_are_failures(tmp_path, body):
    folder = _install_command(tmp_path, "claude", "SessionStart", body)
    assert hd.doctor({"claude": folder})["status"] == "FAIL"


def test_agy_quoted_command_is_not_certified_as_runnable(tmp_path):
    folder = _install_command(tmp_path, "agy", "SessionStart", "print('{}')")
    report = hd.doctor({"agy": folder})
    assert report["status"] == "FAIL"
    assert "does not remove quotes" in report["checks"][0]["detail"]


def test_agy_probe_uses_camelcase_payload_and_injectsteps(tmp_path, monkeypatch):
    def run(command, **kwargs):
        data = json.loads(kwargs["input"])
        assert set(data) == {"conversationId", "workspacePaths", "transcriptPath", "modelName", "lastUserInput"}
        assert "MEMORYMASTER_SHARED_MCP_TOKEN" not in kwargs["env"]
        assert kwargs["env"]["MEMORYMASTER_HOOK_RECALL_REMOTE"] == "0"
        return subprocess.CompletedProcess(command, 0, '{"injectSteps":[{"ephemeralMessage":"fixture"}]}', "")
    monkeypatch.setattr(hd.subprocess, "run", run)
    report = hd._probe("agy", "SessionStart", "python -m memorymaster.surfaces.fixture", 10)
    assert report["status"] == "PASS" and report["injected"]


def test_codex_windows_override_is_probed(tmp_path, monkeypatch):
    folder = tmp_path / "codex config with spaces"
    setup_hooks.install_codex_recall_hooks(
        codex_dir=folder, project_root=tmp_path, data_root=tmp_path,
    )
    config = json.loads((folder / "hooks.json").read_text())
    windows_command = config["hooks"]["SessionStart"][0]["hooks"][0]["commandWindows"]
    assert windows_command == config["hooks"]["SessionStart"][0]["hooks"][0]["command"]

    def run_probe(command, **kwargs):
        invocation = json.loads(command[3])
        event = json.loads(kwargs["input"])["hook_event_name"]
        assert command[0].replace("\\", "/") == sys.executable.replace("\\", "/")
        assert command[1:3] == ["-m", "memorymaster.surfaces.hook_probe"]
        assert invocation["args"] == [
            str((folder / "hooks" / "memorymaster-codex-recall.py").resolve()),
            "--event", event,
        ]
        output = json.dumps({"hookSpecificOutput": {
            "hookEventName": event, "additionalContext": "fixture memory",
        }})
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(hd.subprocess, "run", run_probe)
    monkeypatch.setattr(hd.os, "name", "nt")
    report = hd.doctor({"codex": folder})
    assert report["status"] == "PASS", report
    assert report["checks"][0]["command"] == windows_command
    assert report["checks"][0]["injected"]


def test_codex_inline_hooks_are_probed(tmp_path):
    script = tmp_path / "memorymaster-inline.py"
    script.write_text("print('plain memory')")
    command = f'"{sys.executable}" "{script}"'
    # JSON string quoting is also a valid TOML basic string for this command.
    (tmp_path / "config.toml").write_text('[hooks]\nSessionStart = [{hooks = [{type = "command", command = '
                                           + json.dumps(command) + '}]}]\n')
    report = hd.doctor({"codex": tmp_path})
    assert report["status"] == "PASS", report
    assert report["checks"][0]["injected"]


def test_timeout_is_reported(tmp_path):
    folder = _install_command(tmp_path, "claude", "SessionStart", "import time; time.sleep(10)", timeout=1)
    report = hd.doctor({"claude": folder})
    assert report["status"] == "FAIL"
    assert report["checks"][0]["detail"] == "hook timed out"


def test_scratch_cleanup_failure_does_not_mask_timeout(tmp_path, monkeypatch):
    # T-1973: on Windows a killed/scanned probe can hold a handle in the scratch dir (WinError 32);
    # the resulting PermissionError at cleanup used to replace the timeout verdict.
    real_rmdir = hd.os.rmdir

    def locked_rmdir(path, *args, **kwargs):
        if "memorymaster-hooks-doctor-" in str(path):
            raise PermissionError(13, "simulated sharing violation", str(path))
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(hd.os, "rmdir", locked_rmdir)
    folder = _install_command(tmp_path, "claude", "SessionStart", "import time; time.sleep(10)", timeout=1)
    report = hd.doctor({"claude": folder})
    assert report["checks"][0]["detail"] == "hook timed out"


def test_nonexistent_command_is_still_reported_as_not_executable(tmp_path):
    folder = _install_command(tmp_path, "claude", "SessionStart", "print(1)")
    config = json.loads((folder / "settings.json").read_text())
    handler = config["hooks"]["SessionStart"][0]["hooks"][0]
    handler["command"] = f'"{tmp_path / "python-does-not-exist.exe"}" "{folder / "memorymaster-fixture.py"}"'
    (folder / "settings.json").write_text(json.dumps(config))
    report = hd.doctor({"claude": folder})
    assert report["status"] == "FAIL"
    assert report["checks"][0]["detail"] == "hook command could not be executed"


def test_probe_blocks_external_db_even_when_hook_swallows_failure(tmp_path):
    outside = tmp_path / "must-not-open.db"
    folder = _install_command(tmp_path, "codex", "Stop",
                              f"import sqlite3\ntry: sqlite3.connect({str(outside)!r})\nexcept PermissionError: pass")
    assert hd.doctor({"codex": folder})["status"] == "FAIL"
    assert not outside.exists()


def test_probe_blocks_hardcoded_external_reads(tmp_path):
    outside = tmp_path / "private-config.txt"
    outside.write_text("fixture-secret-never-read")
    folder = _install_command(tmp_path, "codex", "Stop", f"open({str(outside)!r}).read()")
    assert hd.doctor({"codex": folder})["status"] == "FAIL"


def test_probe_blocks_worker_spawn_and_external_write(tmp_path):
    outside = tmp_path / "must-not-write.txt"
    folder = _install_command(tmp_path, "codex", "Stop", f"open({str(outside)!r},'w').write('bad')")
    assert hd.doctor({"codex": folder})["status"] == "FAIL"
    assert not outside.exists()
    (folder / "memorymaster-fixture.py").write_text("import subprocess; subprocess.run(['python','--version'])")
    assert hd.doctor({"codex": folder})["status"] == "FAIL"


def test_installed_claude_recall_uses_shared_core_and_fixture_bootstrap(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "claude"
    monkeypatch.setattr(setup_hooks, "PROJECT_ROOT", tmp_path / "absent-live-root")
    setup_hooks.install_hooks({"provider": "none", "model": "", "api_key": ""}, config_dir=folder)
    settings = json.loads((folder / "settings.json").read_text())
    settings["hooks"] = {"UserPromptSubmit": [settings["hooks"]["UserPromptSubmit"][0]],
                         "SessionStart": settings["hooks"]["SessionStart"]}
    (folder / "settings.json").write_text(json.dumps(settings))
    capsys.readouterr()
    report = hd.doctor({"claude": folder})
    assert report["status"] == "PASS", report
    assert all(check["injected"] for check in report["checks"])
    assert all(check["fixture_memory_seen"] for check in report["checks"])
    assert not (tmp_path / "absent-live-root").exists()


def test_missing_configs_and_opencode_plugin_are_observable(tmp_path):
    report = hd.doctor({"codex": tmp_path / "absent"})
    assert report["status"] == "FAIL"
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "memorymaster.ts").write_text("export default {}")
    report = hd.doctor({"opencode": tmp_path})
    assert report["status"] == "WARN"
    assert report["checks"][0]["plugins"] == ["memorymaster.ts"]


def test_cli_requires_explicit_dirs_and_never_initializes_database(tmp_path, capsys):
    db = tmp_path / "must-not-create.db"
    assert main(["--db", str(db), "--json", "hooks", "doctor", "--codex-dir", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "FAIL"
    assert not db.exists()
    with pytest.raises(SystemExit) as exc:
        main(["hooks", "doctor"])
    assert exc.value.code == 2
