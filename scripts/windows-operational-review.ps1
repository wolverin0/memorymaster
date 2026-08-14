param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath
)

$ErrorActionPreference = "Stop"
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$outputRoot = [IO.Path]::GetFullPath([string]$config.output_root)
[IO.Directory]::CreateDirectory($outputRoot) | Out-Null
$tempJson = Join-Path $outputRoot ("review-{0}.tmp" -f [guid]::NewGuid().ToString("N"))
$tempError = Join-Path $outputRoot ("review-{0}.stderr.tmp" -f [guid]::NewGuid().ToString("N"))
$latestJson = Join-Path $outputRoot "latest.json"
$latestError = Join-Path $outputRoot "latest.stderr.log"
$historyLog = Join-Path $outputRoot "history.jsonl"

$arguments = @(
    "-m", "memorymaster.operations.operational_review",
    "--db", [string]$config.db,
    "--expected-version", [string]$config.expected_version,
    "--lookback-hours", [string]$config.lookback_hours,
    "--canary-query", [string]$config.canary_query,
    "--canary-human-id", [string]$config.canary_human_id,
    "--json"
)

try {
    $output = & ([string]$config.python) @arguments 2> $tempError
    $exitCode = $LASTEXITCODE
    $jsonText = $output -join [Environment]::NewLine
    $null = $jsonText | ConvertFrom-Json
    [IO.File]::WriteAllText($tempJson, $jsonText, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $tempJson -Destination $latestJson -Force
    if ((Get-Item -LiteralPath $tempError).Length -gt 0) {
        Move-Item -LiteralPath $tempError -Destination $latestError -Force
    }
    else {
        Remove-Item -LiteralPath $tempError -Force
        if (Test-Path -LiteralPath $latestError) { Remove-Item -LiteralPath $latestError -Force }
    }
    $record = [ordered]@{
        observed_at = [DateTimeOffset]::Now.ToString("o")
        exit_code = $exitCode
        artifact = $latestJson
        review_performed = $true
    } | ConvertTo-Json -Compress
    [IO.File]::AppendAllText($historyLog, $record + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    exit $exitCode
}
catch {
    if (Test-Path -LiteralPath $tempJson) { Remove-Item -LiteralPath $tempJson -Force }
    if (Test-Path -LiteralPath $tempError) { Move-Item -LiteralPath $tempError -Destination $latestError -Force }
    $record = [ordered]@{
        observed_at = [DateTimeOffset]::Now.ToString("o")
        exit_code = 9
        error_type = $_.Exception.GetType().Name
        review_performed = $false
    } | ConvertTo-Json -Compress
    [IO.File]::AppendAllText($historyLog, $record + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    exit 9
}
