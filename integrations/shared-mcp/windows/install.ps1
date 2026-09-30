# Install the ONE shared local MemoryMaster MCP server (T-0726) on Windows.
# Installs a wheel into the client and scheduled runtimes, writes the service's
# registry config (token generated once, never printed), registers the
# MemoryMaster-MCP-Shared logon task (supervisor launcher), restarts Hermes so it
# runs the same code, and waits for /healthz. Client configs are NOT touched.
# Undo with the release's rollback.ps1.
param(
    [Parameter(Mandatory = $true)][string]$Wheel,
    [Parameter(Mandatory = $true)][string]$Db,
    [Parameter(Mandatory = $true)][string]$WorkspaceAllowlist,
    [string]$ClientPython = 'C:\Users\pauol\AppData\Local\Programs\Python\Python312\python.exe',
    [string]$Runtime = 'C:\Users\pauol\.memorymaster\runtime\graph-profile-20260813',
    [int]$Port = 8766
)
$ErrorActionPreference = 'Stop'
$task = 'MemoryMaster-MCP-Shared'
$key = 'HKCU:\Software\MemoryMaster\SharedMcp'
$launcher = Join-Path $Runtime 'memorymaster-mcp-shared.pyw'
$pythonw = Join-Path $Runtime 'Scripts\pythonw.exe'

foreach ($py in @($ClientPython, (Join-Path $Runtime 'Scripts\python.exe'))) {
    & $py -I -m pip install --no-deps --no-index --force-reinstall --quiet $Wheel
    if ($LASTEXITCODE -ne 0) { throw "pip install failed for $py" }
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'memorymaster-mcp-shared.pyw') -Destination $launcher -Force

if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
$props = Get-ItemProperty -Path $key
if (-not $props.MEMORYMASTER_MCP_HTTP_TOKEN) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    Set-ItemProperty -Path $key -Name MEMORYMASTER_MCP_HTTP_TOKEN -Value $token
    Remove-Variable token, bytes
}
Set-ItemProperty -Path $key -Name MEMORYMASTER_DEFAULT_DB -Value $Db
Set-ItemProperty -Path $key -Name MEMORYMASTER_MCP_DB_ALLOWLIST -Value $Db
Set-ItemProperty -Path $key -Name MEMORYMASTER_MCP_WORKSPACE_ALLOWLIST -Value $WorkspaceAllowlist

if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $task
    Unregister-ScheduledTask -TaskName $task -Confirm:$false
}
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$launcher`"" -WorkingDirectory (Split-Path $Db)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'One shared local MemoryMaster MCP server for every agent session (T-0726). Config: HKCU\Software\MemoryMaster\SharedMcp' | Out-Null
Start-ScheduledTask -TaskName $task

$hermes = Get-ScheduledTask -TaskName 'MemoryMaster-MCP-HTTP-Hermes' -ErrorAction SilentlyContinue
if ($hermes) { Stop-ScheduledTask -TaskName $hermes.TaskName; Start-ScheduledTask -TaskName $hermes.TaskName }

$deadline = (Get-Date).AddSeconds(300)
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://127.0.0.1:$Port/healthz"
        if ($r.StatusCode -eq 200) { Write-Output "shared MCP healthy on 127.0.0.1:$Port"; exit 0 }
    } catch { Start-Sleep -Seconds 3 }
}
throw "shared MCP did not become healthy on 127.0.0.1:$Port within 300 s; see %LOCALAPPDATA%\MemoryMaster\logs\mcp-shared.log"
