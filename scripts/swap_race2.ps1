# C4 finisher v2: kill BOTH holder types (mcp_server AND .claude/hooks python)
# then swap in the same tick. The v1 race only killed mcp_server and left the
# hook process holding the handle.
$ErrorActionPreference = "SilentlyContinue"
Set-Location "G:\_OneDrive\OneDrive\Desktop\Py Apps\memorymaster"
$log = "scripts\swap_race2.log"
"START $(Get-Date -Format o)" | Out-File $log -Encoding ascii

$qc = (@'
import sqlite3;print(sqlite3.connect("memorymaster_repaired.db").execute("PRAGMA quick_check").fetchone()[0])
'@ | & python -)
if ($qc.Trim() -ne "ok") { "ABORT artifact not ok" | Out-File $log -Append -Encoding ascii; exit 1 }
if (-not (Test-Path "memorymaster.db.corrupt-2026-06-05")) {
    Copy-Item -LiteralPath "memorymaster.db" -Destination "memorymaster.db.corrupt-2026-06-05" -Force
}

$done = $false
for ($i = 1; $i -le 120 -and -not $done; $i++) {
    # kill BOTH: mcp_server AND any python running a .claude\hooks script
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match 'memorymaster\.mcp_server' -or $_.CommandLine -match '\.claude[\\/]+hooks' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    # immediate swap attempt
    Remove-Item -LiteralPath "memorymaster.db" -Force
    if (-not (Test-Path "memorymaster.db")) {
        Remove-Item -LiteralPath "memorymaster.db-wal" -Force
        Remove-Item -LiteralPath "memorymaster.db-shm" -Force
        Rename-Item -LiteralPath "memorymaster_repaired.db" -NewName "memorymaster.db" -Force
        if ((Test-Path "memorymaster.db") -and -not (Test-Path "memorymaster_repaired.db")) {
            $done = $true
            "SWAP LANDED iteration $i $(Get-Date -Format o)" | Out-File $log -Append -Encoding ascii
        }
    }
    Start-Sleep -Milliseconds 50
}

if ($done) {
    $v = (@'
import sqlite3
c=sqlite3.connect("memorymaster.db")
print("quick_check="+c.execute("PRAGMA quick_check").fetchone()[0])
print("claims="+str(c.execute("SELECT count(*) FROM claims").fetchone()[0]))
print("verbatim="+str(c.execute("SELECT count(*) FROM verbatim_memories").fetchone()[0]))
'@ | & python -)
    "FINAL:`n$v" | Out-File $log -Append -Encoding ascii
    "DONE" | Out-File $log -Append -Encoding ascii
} else {
    "FAILED after 120 iterations" | Out-File $log -Append -Encoding ascii
}
