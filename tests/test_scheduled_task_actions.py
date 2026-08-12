from __future__ import annotations

import subprocess
from pathlib import Path

from memorymaster.surfaces import setup_hooks


def test_windows_dream_action_uses_pythonw(monkeypatch, tmp_path: Path) -> None:
    python = tmp_path / "python.exe"
    pythonw = tmp_path / "pythonw.exe"
    python.write_bytes(b"")
    pythonw.write_bytes(b"")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(setup_hooks, "IS_WINDOWS", True)
    monkeypatch.setattr(setup_hooks, "PYTHON_EXE", str(python))
    monkeypatch.setattr(setup_hooks, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(setup_hooks.subprocess, "run", fake_run)
    assert setup_hooks.setup_dream_schedule(tmp_path / "memory.db", apply_candidates=True) == "configured"
    action = calls[0][calls[0].index("/tr") + 1]
    assert "pythonw.exe" in action
    assert " -I -m memorymaster.surfaces.scheduled_task dream" in action
    assert "memorymaster.surfaces.scheduled_task dream" in action
    assert "--apply-candidates" in action
    assert "--extract-provider gemini" in action
    assert "--extract-model gemini-3.5-flash" in action
    assert "--consolidate-model zai-coding-plan/glm-5.2" in action
    assert "--clear-provider-variants" in action


def test_windows_dream_schedule_uses_native_fallback_for_long_action(
    monkeypatch, tmp_path: Path,
) -> None:
    python = tmp_path / "python.exe"
    pythonw = tmp_path / "pythonw.exe"
    python.write_bytes(b"")
    pythonw.write_bytes(b"")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "schtasks":
            raise subprocess.CalledProcessError(1, command, stderr="action too long")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(setup_hooks, "IS_WINDOWS", True)
    monkeypatch.setattr(setup_hooks, "PYTHON_EXE", str(python))
    monkeypatch.setattr(setup_hooks, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(setup_hooks.subprocess, "run", fake_run)

    result = setup_hooks.setup_dream_schedule(
        tmp_path / "memory.db", apply_candidates=True,
    )

    assert result == "configured"
    assert len(calls) == 2
    assert calls[1][0][0].lower().endswith("powershell.exe")
    assert "New-ScheduledTaskAction" in calls[1][0][-1]


def test_verify_reports_action_last_result_queue_and_provider(monkeypatch, tmp_path: Path) -> None:
    output = (
        "Task To Run: C:\\Python\\pythonw.exe -m memorymaster.surfaces.scheduled_task dream\r\n"
        "Last Result: 0\r\n"
    )
    monkeypatch.setattr(setup_hooks, "IS_WINDOWS", True)
    monkeypatch.setattr(
        setup_hooks.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, output, ""),
    )
    report = setup_hooks.verify_scheduled_automation(tmp_path / "missing.db")
    assert report["dreaming"]["hidden_execution"] is True
    assert report["dreaming"]["last_result"] == "0"
    assert report["queue_depth"] == {}
    assert report["capture_coverage"]["status"] == "unavailable"
    assert "claim_extractor" in report["provider_readiness"]


def test_provider_readiness_uses_opencode_for_dream_extraction(monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_DREAM_EXTRACT_PROVIDER", "opencode")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(
        setup_hooks.shutil,
        "which",
        lambda command: "C:\\Tools\\opencode.cmd" if command == "opencode" else None,
    )

    readiness = setup_hooks._provider_readiness()

    assert readiness["dream_extractor"] is True
    assert readiness["dream_consolidator"] is True
    assert readiness["claim_extractor"] is True
    assert readiness["graph_extractor"] is True


def test_provider_readiness_accepts_ready_capture_fallback(monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "google")
    monkeypatch.setenv("MEMORYMASTER_LLM_FALLBACK_PROVIDER", "opencode")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(setup_hooks.Path, "is_file", lambda _path: False)
    monkeypatch.setattr(
        setup_hooks.shutil,
        "which",
        lambda command: "C:\\Tools\\opencode.cmd" if command == "opencode" else None,
    )

    readiness = setup_hooks._provider_readiness()

    assert readiness["claim_extractor"] is True
    assert readiness["graph_extractor"] is True


def test_verify_reads_full_task_action_from_xml(monkeypatch, tmp_path: Path) -> None:
    list_output = (
        "Task To Run: C:\\Python\\pythonw.exe -m memorymaster.surfaces.scheduled_task dream\r\n"
        "Last Result: 0\r\n"
    )
    xml_output = """<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Actions>
    <Exec>
      <Command>C:\\Python\\pythonw.exe</Command>
      <Arguments>-m memorymaster.surfaces.scheduled_task dream --apply-candidates</Arguments>
    </Exec>
  </Actions>
</Task>
"""

    def fake_run(command, **kwargs):
        output = xml_output if "/xml" in command else list_output
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(setup_hooks, "IS_WINDOWS", True)
    monkeypatch.setattr(setup_hooks.subprocess, "run", fake_run)

    report = setup_hooks.verify_scheduled_automation(
        tmp_path / "missing.db",
        apply_candidates=True,
    )

    assert report["mode_matches"] is True
    assert "--apply-candidates" in report["dreaming"]["task_action"]
