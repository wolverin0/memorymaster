<#
.SYNOPSIS
  C4 of the corruption-repair plan: swap the verified memorymaster_repaired.db in
  for the corrupt memorymaster.db. DESTRUCTIVE to live writers.

  This is the ONLY repair step that cannot run non-destructively: Windows refuses
  to move/rename memorymaster.db while any process holds an open handle, and ~12
  per-pane "python -m memorymaster.mcp_server" processes (children of live
  Claude/Codex panes) keep it open. So the swap REQUIRES stopping those writers.

  default (-WhatIf): prints what it would do, kills nothing, moves nothing.
  -Apply         : actually stop the mcp_server writers, verify no handles, swap.

  Run ONLY with operator authorization - it terminates other agents' MCP servers
  (their parent panes survive and can /mcp reconnect).
#>
param([switch]$Apply)

$ErrorActionPreference = "Stop"
Set-Location "G:\_OneDrive\OneDrive\Desktop\Py Apps\memorymaster"

function Get-MmWriters {
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match 'memorymaster\.mcp_server' })
}

$repaired = "memorymaster_repaired.db"
if (-not (Test-Path $repaired)) { throw "Repaired artifact not found - run scripts/recover_db_indexcorrupt.py first." }

# Re-verify the artifact is clean RIGHT BEFORE swapping.
$verify = @'
import sqlite3
c = sqlite3.connect("memorymaster_repaired.db")
print(c.execute("PRAGMA quick_check").fetchone()[0])
'@
$qc = ($verify | & python -)
if ($qc.Trim() -ne "ok") { throw "Artifact quick_check is NOT ok: $qc - refusing to swap." }
Write-Output "Artifact quick_check: ok"

$writers = Get-MmWriters
Write-Output "Live memorymaster.mcp_server writers: $($writers.Count)"
$writers | ForEach-Object { Write-Output ("  PID {0} parent={1}" -f $_.ProcessId, $_.ParentProcessId) }

if (-not $Apply) {
    Write-Output ""
    Write-Output "[WhatIf] Would stop $($writers.Count) mcp_server procs, confirm 0 handles, then:"
    Write-Output "[WhatIf]   move memorymaster.db -> memorymaster.db.corrupt-2026-06-05"
    Write-Output "[WhatIf]   delete memorymaster.db-wal / -shm"
    Write-Output "[WhatIf]   move memorymaster_repaired.db -> memorymaster.db"
    Write-Output "[WhatIf] Re-run with -Apply to execute (operator authorization required)."
    return
}

Write-Output "APPLY: stopping mcp_server writers..."
foreach ($w in $writers) {
    try { Stop-Process -Id $w.ProcessId -Force; Write-Output ("  stopped PID {0}" -f $w.ProcessId) }
    catch { Write-Output ("  WARN PID {0}: {1}" -f $w.ProcessId, $_) }
}
Start-Sleep -Seconds 3
$still = Get-MmWriters
if ($still.Count -gt 0) { throw "ABORT: $($still.Count) writers still alive after stop - not swapping." }
Write-Output "All writers stopped."

Move-Item memorymaster.db "memorymaster.db.corrupt-2026-06-05" -Force
foreach ($s in @("memorymaster.db-wal", "memorymaster.db-shm")) { if (Test-Path $s) { Remove-Item $s -Force } }
Move-Item memorymaster_repaired.db memorymaster.db -Force

$final = @'
import sqlite3
c = sqlite3.connect("memorymaster.db")
print("quick_check:", c.execute("PRAGMA quick_check").fetchone()[0])
print("claims:", c.execute("SELECT count(*) FROM claims").fetchone()[0])
print("verbatim:", c.execute("SELECT count(*) FROM verbatim_memories").fetchone()[0])
'@
Write-Output "Swapped. Final verify:"
$final | & python -
Write-Output "DONE. Corrupt DB preserved at memorymaster.db.corrupt-2026-06-05. Reopen panes / /mcp reconnect."
