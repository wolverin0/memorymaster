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
    assert "memorymaster.surfaces.scheduled_task dream" in action
    assert "--apply-candidates" in action


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
    assert "claim_extractor" in report["provider_readiness"]
