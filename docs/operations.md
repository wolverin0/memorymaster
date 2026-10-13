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

## Prompt recall hook: volume, repeats and the per-lane switch

The UserPromptSubmit recall hook (T-1351, decided 2026-10-13) injects at most
2 claims per prompt, dense and lexical (`MEMORYMASTER_RECALL_HOOK_MAX_CLAIMS`).
Dense thresholds stay at cosine >= 0.68 (other projects >= 0.76): raising them
bought no precision on the fixed set, only fewer injections. All three are
configurable (`MEMORYMASTER_RECALL_DENSE_MIN_SCORE`, `..._OUTSIDE_MIN_SCORE`,
`MEMORYMASTER_RECALL_LEXICAL_MIN_RELEVANCE`, default 0.5 for lexical rows, 0 turns it off). A claim
injected in a session is not injected again in it: the ids (never text) are kept
per session under `~/.memorymaster/hook_state/recall_seen/`, at most 200 ids per
session and 400 sessions, expiring after 12 hours and cleared by SessionStart.
`MEMORYMASTER_RECALL_SESSION_DEDUP=0` disables it. The MCP-Shared service
applies the cap and the dedup, so restart it after deploying.

Turn the hook off for some projects, default on, without touching code or
restarting anything: write `~/.memorymaster/recall-hook.json`. The wisp lane:

```json
{"disabled_cwds": ["whatsappbot-final", "whatsappbot", "bot-rf-optimizer"]}
```

An entry with no path separator matches any folder of the session's cwd (so
worktrees under that repo are off too); an entry with separators matches that
path and its subtree. `{"enabled": false}` turns the hook off everywhere. The
file is read on every prompt; delete it (or the entries) to turn the hook back
on. A skipped prompt shows `via=off` in the hook path and injects nothing.

Measure the effect with `scripts/eval_prompt_recall.py` (fixed 200-prompt set,
dense and lexical) and, after a week, with `scripts/review_recall_citations.py`
(citation rate and injected characters per prompt, lanes on vs off).
