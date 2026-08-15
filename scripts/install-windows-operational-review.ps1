param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,
    [Parameter(Mandatory = $true)]
    [string]$Database,
    [string]$ExpectedVersion = "4.7.5",
    [int]$EveryHours = 6,
    [int]$LookbackHours = 8,
    [string]$CanaryQuery = "why does wezterm cli time out from Node but not from bash",
    [string]$CanaryHumanId = "mm-8aef",
    [string]$TaskName = "MemoryMaster-Operational-Review"
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { throw "Python executable not found" }
if (-not (Test-Path -LiteralPath $Database -PathType Leaf)) { throw "Database not found" }
if ($EveryHours -lt 1 -or $EveryHours -gt 24) { throw "EveryHours must be between 1 and 24" }

$installRoot = Join-Path $env:LOCALAPPDATA "MemoryMaster\operational-review"
$outputRoot = Join-Path $installRoot "results"
[IO.Directory]::CreateDirectory($installRoot) | Out-Null
[IO.Directory]::CreateDirectory($outputRoot) | Out-Null
$runner = Join-Path $installRoot "windows-operational-review.ps1"
$configPath = Join-Path $installRoot "config.json"
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "windows-operational-review.ps1") -Destination $runner -Force

$config = [ordered]@{
    python = [IO.Path]::GetFullPath($PythonExe)
    db = [IO.Path]::GetFullPath($Database)
    expected_version = $ExpectedVersion
    lookback_hours = $LookbackHours
    canary_query = $CanaryQuery
    canary_human_id = $CanaryHumanId
    output_root = $outputRoot
}
[IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json), [Text.UTF8Encoding]::new($false))

$actionArgs = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -ConfigPath `"$configPath`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $actionArgs
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Hours $EveryHours)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

[pscustomobject]@{
    task_name = $TaskName
    interval_hours = $EveryHours
    config = $configPath
    result = (Join-Path $outputRoot "latest.json")
    history = (Join-Path $outputRoot "history.jsonl")
} | ConvertTo-Json -Compress
