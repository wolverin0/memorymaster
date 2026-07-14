# MemoryMaster Operations

MemoryMaster's operational tools are fail-closed and use disposable targets by
default. They never upload a backup, erase product data, or run PostgreSQL
restore commands automatically.

## Recovery contract

- SQLite backups use the online backup API, validate `integrity_check` and
  foreign keys, encrypt before leaving temporary local storage, and write an
  authenticated checksum manifest.
- Configure `MEMORYMASTER_BACKUP_KEY` with a Fernet key through the deployment
  secret manager. The key is intentionally not accepted on the command line.
- Default objectives are RPO 24 hours and RTO 30 minutes. Override them only
  with an operator-approved service objective.
- `memorymaster-ops backup-create --db DB --destination BACKUP --off-device`
  asserts that the operator-selected destination is off-device; the tool does
  not infer storage topology.
- `memorymaster-ops restore-drill --backup BACKUP` restores only into a
  disposable temporary database and reruns full integrity and foreign-key
  checks.
- PostgreSQL remains plan-only until separate dump and restore endpoints,
  `pg_dump`/`pg_restore`, and a disposable restricted-role database are
  supplied.

## Health and alerts

`memorymaster-ops health` evaluates backup age, retry backlog, provider
failures, DB integrity, WAL size, disk use, OpenTelemetry readiness, and error
tracking readiness. With `--persist`, one aggregate append-only system event is
written with the owner and runbook. Alert delivery is configured externally;
the persisted snapshot remains the source of evidence across restarts.

## Privacy operations

`memorymaster-ops privacy-plan` is always dry-run. It inventories attributable
primary DB and verbatim rows and explicitly reports Qdrant, artifacts, spool,
wiki, caches, and backups that cannot be proven complete. Backups expire by
policy; they are never deleted immediately by an erasure request. Any apply
workflow requires a verified backup, legal/product approval, an exact tenant
and principal selector, and separate live-data authorization.
