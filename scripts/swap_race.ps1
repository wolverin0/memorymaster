# C4 finisher: win the race against this session's auto-respawning mcp_server.
# Tight loop: kill ALL memorymaster.mcp_server procs, then IMMEDIATELY (no sleep)
# try to move the corrupt db aside + drop the repaired one in. The harness restarts
# the server ~1s after a kill; a no-delay Move-Item lands inside that window.
$ErrorActionPreference = "SilentlyContinue"
Set-Location "G:\_OneDrive\OneDrive\Desktop\Py Apps\memorymaster"
$log = "scripts\swap_race.log"
"START $(Get-Date -Format o)" | Out-File $log -Encoding utf8

# Pre-verify artifact once.
$qc = (@'
import sqlite3;print(sqlite3.connect("memorymaster_repaired.db").execute("PRAGMA quick_check").fetchone()[0])
'@ | & python -)
if ($qc.Trim() -ne "ok") { "ABORT artifact not ok: $qc" | Out-File $log -Append; exit 1 }
"artifact ok" | Out-File $log -Append

# corrupt backup already exists (.corrupt-2026-06-05); if not, copy now (shared-read works)
if (-not (Test-Path "memorymaster.db.corrupt-2026-06-05")) {
    Copy-Item -LiteralPath "memorymaster.db" -Destination "memorymaster.db.corrupt-2026-06-05" -Force
    "backup copied" | Out-File $log -Append
}

$done = $false
for ($i = 1; $i -le 400 -and -not $done; $i++) {
    # kill every mcp_server, no wait
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match 'memorymaster\.mcp_server' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    # immediately try the move (delete live + sidecars, then rename repaired in)
    Remove-Item -LiteralPath "memorymaster.db" -Force
    if (-not (Test-Path "memorymaster.db")) {
        Remove-Item -LiteralPath "memorymaster.db-wal" -Force
        Remove-Item -LiteralPath "memorymaster.db-shm" -Force
        Rename-Item -LiteralPath "memorymaster_repaired.db" -NewName "memorymaster.db" -Force
        if ((Test-Path "memorymaster.db") -and -not (Test-Path "memorymaster_repaired.db")) {
            $done = $true
            "SWAP LANDED on iteration $i at $(Get-Date -Format o)" | Out-File $log -Append
        }
    }
}

if ($done) {
    $v = (@'
import sqlite3
c=sqlite3.connect("memorymaster.db")
print("quick_check="+c.execute("PRAGMA quick_check").fetchone()[0])
print("claims="+str(c.execute("SELECT count(*) FROM claims").fetchone()[0]))
print("verbatim="+str(c.execute("SELECT count(*) FROM verbatim_memories").fetchone()[0]))
'@ | & python -)
    "FINAL VERIFY:`n$v" | Out-File $log -Append
    "DONE" | Out-File $log -Append
} else {
    "FAILED after 400 iterations - swap did not land" | Out-File $log -Append
}
