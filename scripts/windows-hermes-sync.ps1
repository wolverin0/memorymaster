# =============================================================================
# windows-hermes-sync.ps1 - incremental delta sync, Windows side.
# =============================================================================
#
# Pairs with scripts/hermes-sync.sh on the Hermes (Ubuntu VM) side.
#
# Integration model (Option 1, decided 2026-05-18):
#   - Windows OWNS all writes to the Windows MemoryMaster DB. This script is
#     the ONLY thing that runs merge-db against the Windows DB.
#   - Hermes never writes the Windows DB and never copies the full 2.5 GB DB.
#
# This script (run twice daily via Task Scheduler) does two things:
#   1. INBOUND  - merge Hermes's delta (delta-exchange\hermes-delta.db) into
#                 the Windows MemoryMaster DB.
#   2. OUTBOUND - export the Windows delta to delta-exchange\windows-delta.db
#                 for Hermes to consume on its next run.
#
# Both the Windows DB and the delta files live on local disk (the G:\ repo).
# The "share" is just that same directory exposed over SMB to Hermes - from
# THIS script's point of view every path is local, so SQLite-over-network is
# a non-issue here. (The constraint matters on the Hermes side, handled there.)
#
# Watermark: delta-exchange\.windows.watermark holds the newest updated_at
# already exported. export-delta uses >= so a boundary claim may re-export;
# merge-db on the Hermes side is idempotent, so that is harmless.
#
# Install as two scheduled tasks (run once; a per-user task needs no admin).
# See scripts/install-windows-hermes-sync.md for the exact schtasks commands.
# Recommended times: 04:00 and 16:00 - one hour after Hermes's 03:00/15:00
# runs, so Hermes's fresh hermes-delta.db is already on the share.
#
# Environment variables (optional overrides):
#   MEMORYMASTER_DB  - path to the Windows MemoryMaster DB
#   MEMORYMASTER_BIN - memorymaster CLI invocation (default: "python -m memorymaster")
# =============================================================================

$ErrorActionPreference = "Stop"

# --- configuration ----------------------------------------------------------
$WinDb = $env:MEMORYMASTER_DB
if (-not $WinDb) {
    $WinDb = "G:\_OneDrive\OneDrive\Desktop\Py Apps\memorymaster\memorymaster.db"
}
$MmBin = $env:MEMORYMASTER_BIN
if (-not $MmBin) { $MmBin = "python -m memorymaster" }

$RepoDir       = Split-Path $WinDb -Parent
$ExchangeDir   = Join-Path $RepoDir "delta-exchange"
$HermesDelta   = Join-Path $ExchangeDir "hermes-delta.db"
$WindowsDelta  = Join-Path $ExchangeDir "windows-delta.db"
$WatermarkFile = Join-Path $ExchangeDir ".windows.watermark"
$LockFile      = Join-Path $ExchangeDir ".windows-sync.lock"

New-Item -ItemType Directory -Force -Path $ExchangeDir | Out-Null

function Log($msg) { Write-Output ("{0}  {1}" -f (Get-Date -Format o), $msg) }

# --- single-flight ----------------------------------------------------------
# A stale lock older than 1h is assumed dead (a crashed prior run) and cleared.
if (Test-Path $LockFile) {
    $age = (Get-Date) - (Get-Item $LockFile).LastWriteTime
    if ($age.TotalHours -lt 1) {
        Log "SKIP: another windows sync in progress (lock age $([int]$age.TotalMinutes)m)"
        exit 0
    }
    Log "stale lock ($([int]$age.TotalHours)h old) - clearing"
    Remove-Item $LockFile -Force
}
New-Item -ItemType File -Path $LockFile -Force | Out-Null

try {
    Log "START windows delta sync"

    # Run memorymaster and return parsed JSON. $MmBin may be multi-word
    # ("python -m memorymaster"); split it so the first token is the exe.
    function Invoke-Mm([string[]]$mmArgs) {
        $parts = $MmBin.Split(" ", [StringSplitOptions]::RemoveEmptyEntries)
        $exe   = $parts[0]
        $base  = @($parts[1..($parts.Length-1)]) + @("--json")
        $raw = & $exe @base @mmArgs 2>&1 | Out-String
        try {
            return $raw | ConvertFrom-Json
        } catch {
            throw "memorymaster did not return JSON. Raw output:`n$raw"
        }
    }

    # === 1. INBOUND: merge Hermes's delta into the Windows DB ================
    if (Test-Path $HermesDelta) {
        Log "merging hermes-delta.db into Windows DB..."
        $merge = Invoke-Mm @("--db", $WinDb, "merge-db", "--source", $HermesDelta)
        if ($merge.ok) {
            $d = $merge.data
            Log ("  merged={0} skipped={1} errors={2}" -f $d.merged, $d.skipped, $d.errors)
        } else {
            throw "merge-db failed: $($merge.error)"
        }
    } else {
        Log "no hermes-delta.db on the share yet - skipping inbound merge"
    }

    # === 2. OUTBOUND: export the Windows delta for Hermes ===================
    $since = ""
    if (Test-Path $WatermarkFile) {
        # Strip a leading UTF-8 BOM defensively. An older build wrote the
        # watermark with `Set-Content -Encoding utf8`, which prepends a BOM
        # in Windows PowerShell 5.1. .Trim() does NOT remove a BOM (U+FEFF
        # is not whitespace); a BOM-prefixed timestamp passed to
        # `export-delta --since` sorts above every real timestamp, so the
        # watermark would match nothing and every run would export 0 claims.
        $since = (Get-Content $WatermarkFile -Raw).Trim().TrimStart([char]0xFEFF).Trim()
    }
    Log ("exporting Windows delta since '{0}'..." -f $(if ($since) { $since } else { "(full)" }))

    # Export to a temp name, then atomic-rename - Hermes may be reading the
    # share concurrently; never let it see a half-written windows-delta.db.
    # Omit --since entirely when the watermark is empty: PowerShell drops a
    # bare "" argument, which would collapse the command line. The CLI's
    # --since default is "" (full export), so omitting it is equivalent.
    $tmpDelta = "$WindowsDelta.new"
    $exportArgs = @("--db", $WinDb, "export-delta", "--output", $tmpDelta)
    if ($since) { $exportArgs += @("--since", $since) }
    $export = Invoke-Mm $exportArgs
    if (-not $export.ok) {
        if (Test-Path $tmpDelta) { Remove-Item $tmpDelta -Force }
        throw "export-delta failed: $($export.error)"
    }
    Move-Item -Path $tmpDelta -Destination $WindowsDelta -Force

    $ed = $export.data
    Log ("  exported={0} citations={1}" -f $ed.exported, $ed.citations)

    # Advance the watermark only when the export actually saw rows.
    if ($ed.max_updated_at) {
        # Write with -Encoding ascii: an ISO-8601 timestamp is pure ASCII,
        # and ascii encoding has NO byte-order mark. `Set-Content -Encoding
        # utf8` in Windows PowerShell 5.1 prepends a BOM, which would
        # corrupt the watermark on the next read.
        Set-Content -Path $WatermarkFile -Value $ed.max_updated_at -Encoding ascii -NoNewline
        Log ("  advanced windows watermark -> {0}" -f $ed.max_updated_at)
    } else {
        Log "  delta is empty - watermark unchanged"
    }

    Log "DONE windows delta sync"
}
finally {
    if (Test-Path $LockFile) { Remove-Item $LockFile -Force }
}
