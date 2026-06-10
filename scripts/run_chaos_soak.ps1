# Chaos soak runner -- P1 WAL-discipline exit gate (spec section 4, step 12).
#
# Builds a schema-identical fixture slice from the live DB (READ-ONLY -- the
# live file is never written, never soaked against) and runs the kill-round
# soak in both flag modes. Pass gate: 0 quick_check failures, 0 FK orphans,
# 0 lost acked writes, flag OFF and ON (reports land next to each run's
# tmp dir as soak-report.json; pytest prints their paths on failure).
#
# Usage (from the repo root or anywhere):
#   powershell -File scripts\run_chaos_soak.ps1                  # both modes, 20x60s rounds
#   powershell -File scripts\run_chaos_soak.ps1 -Mode on -Rounds 5 -RoundSeconds 30
#   powershell -File scripts\run_chaos_soak.ps1 -RebuildSlice    # refresh the fixture slice
#
# The gated run happens AFTER merge (spec section 3 step 12) -- this script is the
# operator entry point for that run.

param(
    [string]$SourceDb = "",
    [string]$SlicePath = (Join-Path $env:LOCALAPPDATA "memorymaster\soak\soak-slice.db"),
    [int]$Rounds = 20,
    [int]$RoundSeconds = 60,
    [ValidateSet("off", "on", "both")]
    [string]$Mode = "both",
    [switch]$RebuildSlice,
    [int]$MaxClaims = 20000,
    [int]$MaxVerbatim = 50000
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$harness = Join-Path $repoRoot "tests\soak\chaos_soak.py"
if (-not (Test-Path $harness)) {
    Write-Error "harness not found: $harness"
    exit 1
}

if ($SourceDb -eq "") {
    $SourceDb = Join-Path $repoRoot "memorymaster.db"
}

# 1. Fixture slice: built read-only from the live DB, reused across runs.
if ($RebuildSlice -or -not (Test-Path $SlicePath)) {
    if (-not (Test-Path $SourceDb)) {
        Write-Error "source DB not found: $SourceDb (pass -SourceDb)"
        exit 1
    }
    Write-Host "[soak] building fixture slice from $SourceDb -> $SlicePath (read-only source)"
    python $harness --build-slice --source $SourceDb --dest $SlicePath --max-claims $MaxClaims --max-verbatim $MaxVerbatim
    if ($LASTEXITCODE -ne 0) {
        Write-Error "slice build failed (exit $LASTEXITCODE)"
        exit 1
    }
} else {
    Write-Host "[soak] reusing fixture slice: $SlicePath (pass -RebuildSlice to refresh)"
}

# 2. Soak parameters travel via env (read by tests/soak/chaos_soak.py).
$env:MM_SOAK_DB_SLICE = $SlicePath
$env:MM_SOAK_ROUNDS = "$Rounds"
$env:MM_SOAK_ROUND_SECS = "$RoundSeconds"

# 3. Mode matrix: flag OFF (legacy regression guard) and flag ON (new regime).
#    The tests set MEMORYMASTER_WAL_DISCIPLINE themselves per test.
$selector = switch ($Mode) {
    "off" { "test_chaos_soak_flag_off" }
    "on" { "test_chaos_soak_flag_on" }
    default { "test_chaos_soak" }
}

Write-Host "[soak] rounds=$Rounds round_seconds=$RoundSeconds mode=$Mode"
Push-Location $repoRoot
try {
    python -m pytest $harness -q -p no:cacheprovider --tb=short -k $selector
    $soakExit = $LASTEXITCODE
} finally {
    Pop-Location
    Remove-Item Env:MM_SOAK_DB_SLICE -ErrorAction SilentlyContinue
    Remove-Item Env:MM_SOAK_ROUNDS -ErrorAction SilentlyContinue
    Remove-Item Env:MM_SOAK_ROUND_SECS -ErrorAction SilentlyContinue
}

if ($soakExit -ne 0) {
    Write-Error "[soak] GATE FAILED (pytest exit $soakExit) -- see soak-report.json in the pytest tmp dirs"
    exit $soakExit
}
Write-Host "[soak] GATE PASSED: 0 quick_check failures, 0 FK orphans, 0 lost acked writes ($Mode)"
exit 0
