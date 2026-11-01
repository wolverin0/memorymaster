param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath
)

$ErrorActionPreference = "Stop"
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
# The Python supervisor owns exactly one review child, its deadline and atomic evidence.
& ([string]$config.python) -m memorymaster.operations.review_supervisor --config $ConfigPath
$rc = $LASTEXITCODE
# Operator 2026-10-29: 3 = WARN (real findings, recorded in results/latest.json) counts as a successful run
# for Task Scheduler; 1 = FAIL stays red.
if ($rc -eq 3) { exit 0 }
exit $rc
