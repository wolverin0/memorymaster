param(
  [string]$Dsn = "postgresql://mm:mm_pw@127.0.0.1:6543/memorymaster?connect_timeout=5",
  [string]$Workspace = "."
)

$ErrorActionPreference = "Stop"

python -m memorymaster --db $Dsn --workspace $Workspace init-db
python -m memorymaster --db $Dsn --workspace $Workspace ingest --text "Server IP is 192.168.100.186" --subject server --predicate ip_address --object 192.168.100.186 --source "session://chat|turn-1|smoke"
python -m memorymaster --db $Dsn --workspace $Workspace ingest --text "Server IP is 10.0.0.2" --subject server --predicate ip_address --object 10.0.0.2 --source "session://chat|turn-2|smoke"
python -m memorymaster --db $Dsn --workspace $Workspace run-cycle --policy-mode cadence --policy-limit 100 --min-citations 1 --min-score 0.5
python -m memorymaster --db $Dsn --workspace $Workspace query "server ip" --retrieval-mode hybrid --limit 10
