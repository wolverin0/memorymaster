# Hermes MemoryMaster provider
# Covers: standalone Hermes MemoryProvider installation, authority transport, durable outbox, and rollback.
# Key terms: MemoryProvider, MCP HTTP, Windows authority, VM replica, session scope, replay safety.
# Read when: installing or operating the Hermes companion without modifying Hermes core.
# Authority: MemoryMaster on Windows is the only writer; the VM SQLite replica is recall-only fallback.
# Safety: tokens stay in environment files, raw session IDs never persist, global scope is forbidden.
# Status: local implementation; do not activate against live Hermes until MemoryMaster P4 gates pass.

This package implements Hermes Agent's supported external `MemoryProvider` ABI.
It sends authenticated streamable-MCP calls to the authoritative Windows
MemoryMaster service and keeps a bounded SQLite outbox under `HERMES_HOME`.

## Install in a disposable Hermes profile

```bash
python -m pip install ./integrations/hermes-memorymaster
export MEMORYMASTER_HERMES_MCP_URL='http://windows-host:8765/mcp'
export MEMORYMASTER_HERMES_MCP_TOKEN='read-from-a-private-env-file'
hermes-memorymaster install --hermes-home "$HERMES_HOME"
hermes-memorymaster install --hermes-home "$HERMES_HOME" --apply
hermes memory setup
# choose memorymaster
hermes memorymaster status
```

The first installer call is a no-write preview. The second writes only three
shim files under `$HERMES_HOME/plugins/memorymaster/`; it does not edit Hermes
core or `config.yaml`. This directory path matches Hermes commit
`7cf71c32bbd27ac4044b6b6a5f0c280268e7ecb5`. That build's general pip plugin
loader cannot register exclusive memory providers, so this package deliberately
uses the supported user-provider directory instead of a misleading entry point.

## Behavior

- `sync_turn()` sanitizes the completed turn, hashes its session identity,
  commits it to `memorymaster-outbox.db`, signals a daemon worker, and returns.
- The worker retries network failures with exponential backoff, bounded jitter,
  five attempts, and a circuit breaker. Authentication and scope errors block.
- MemoryMaster remains the deduplication authority through producer, external,
  session, turn, and content identities.
- `prefetch()` is non-blocking and cache-backed. A configured VM replica may
  answer recall during authority downtime, but it exposes no write operation.
- The authoritative recall transport defaults to a 350 ms hard timeout for the
  live injection path; operators may tune it in the non-secret config.
- Built-in memory additions are mirrored as queued evidence. Removal is always
  a `forget(..., apply=False)` preview.

## Configuration

Secrets belong only in the Hermes environment:

```bash
MEMORYMASTER_HERMES_MCP_URL=http://windows-host:8765/mcp
MEMORYMASTER_HERMES_MCP_TOKEN=replace-at-install-time
```

Non-secret options live in `$HERMES_HOME/memorymaster-provider.json`:

```json
{
  "default_scope": "user",
  "outbox": "memorymaster-outbox.db",
  "replica_db": "/srv/memorymaster/replica.db",
  "replica_workspace": "/srv/memorymaster"
}
```

`default_scope` accepts only `user` or `project:<slug>`. Prefer the
`memorymaster_scope` tool to bind a specific live session explicitly.

## Rollback

Run `hermes memory off` or restore the prior `memory.provider` value, then
restart only the Hermes gateway service. Leave the outbox in place until its
pending/retryable count is zero or the operator has reviewed every blocked row.
