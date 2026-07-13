param(
  [string]$Dsn = $env:MEMORYMASTER_POSTGRES_DSN,
  [string]$Workspace = "."
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Dsn)) {
  throw "Set MEMORYMASTER_POSTGRES_DSN to an operator-supplied disposable PostgreSQL DSN."
}

python -m memorymaster --db $Dsn --workspace $Workspace init-db
python -m memorymaster --db $Dsn --workspace $Workspace ingest --text "Server endpoint is primary" --subject server --predicate endpoint --object primary --source "session://chat|turn-1|smoke"
python -m memorymaster --db $Dsn --workspace $Workspace ingest --text "Server endpoint is standby" --subject server --predicate endpoint --object standby --source "session://chat|turn-2|smoke"
python -m memorymaster --db $Dsn --workspace $Workspace run-cycle --policy-mode cadence --policy-limit 100 --min-citations 1 --min-score 0.5
python -m memorymaster --db $Dsn --workspace $Workspace query "server endpoint" --retrieval-mode hybrid --limit 10
