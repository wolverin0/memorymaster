param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath
)

$ErrorActionPreference = "Stop"
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
# The Python supervisor owns exactly one review child, its deadline and atomic evidence.
& ([string]$config.python) -m memorymaster.operations.review_supervisor --config $ConfigPath
exit $LASTEXITCODE
