"""Execute the installer with disposable files and intercepted task registration."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.skipif(not shutil.which("powershell"), reason="Windows task installer")
def test_review_task_gets_normal_io_and_keeps_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    db = tmp_path / "fixture.db"
    db.touch()
    receipt = tmp_path / "registration.json"
    harness = tmp_path / "intercept-installer.ps1"
    harness.write_text(r'''
param($Installer, $Python, $Database, $Receipt)
$ErrorActionPreference = "Stop"
function New-ScheduledTaskAction { param($Execute, $Argument) @{} }
function New-ScheduledTaskTrigger { param([switch]$Once, $At, $RepetitionInterval) @{} }
function New-ScheduledTaskSettingsSet {
    param($MultipleInstances, $ExecutionTimeLimit, [switch]$StartWhenAvailable, [int]$Priority = 7)
    @{ priority = $Priority; deadline_minutes = $ExecutionTimeLimit.TotalMinutes;
       multiple_instances = $MultipleInstances }
}
function Register-ScheduledTask {
    param($TaskName, $Action, $Trigger, $Settings, [switch]$Force)
    $Settings | ConvertTo-Json | Set-Content -LiteralPath $Receipt -Encoding UTF8
}
& $Installer -PythonExe $Python -Database $Database -TaskName 'disposable-intercepted-task'
''', encoding="utf-8")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-File", str(harness),
         str(Path("scripts/install-windows-operational-review.ps1").resolve()),
         sys.executable, str(db), str(receipt)], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    registered = json.loads(receipt.read_text(encoding="utf-8-sig"))
    assert registered == {"priority": 6, "deadline_minutes": 25, "multiple_instances": "IgnoreNew"}
