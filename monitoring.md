---
project: memorymaster
path: G:\_OneDrive\OneDrive\Desktop\Py Apps\memorymaster
stack: Python 3.10+ / SQLite+FTS5 / Qdrant (optional) / FastMCP
repo: https://github.com/wolverin0/memorymaster
entry_point: "python -m memorymaster --db memorymaster.db <command>"
test_command: "python -m pytest tests/ -q --tb=short"
build_command: "python -m build"
health_check: "python -c 'from memorymaster.storage import SQLiteStore; s=SQLiteStore(\"memorymaster.db\"); s.init_db(); print(\"ok\")'"
mcp_servers: "memorymaster, gitnexus, serena, context7"
# Note: graphify-out/ present as of 2026-04-10 — 1434 nodes, 2830 edges, 83 communities, 10 god nodes

# === OmniClaude operational config ===
omniclaude:
  active_hours: "09:00-24:00 ART"
  max_actions_per_hour: 3
  max_concurrent_monitors: 2
  escalation_channel: "telegram:2128295779"
  watchdog: "while-loop:scripts/omniclaude-forever.sh"
  # state_checkpoint: ".omniclaude/state.json"   # OPTIONAL (v2+)
---

## What This Project Does

MemoryMaster is a production-grade memory reliability system for AI coding
agents. It provides lifecycle-managed claims with citations, conflict
detection, steward governance, an LLM wiki engine (compiled truth + timeline),
and a 7-hook stack that automates recall/classify/validate/session-start/
auto-ingest/pre-compact. Published to PyPI as `memorymaster` (latest 3.3.0).

## Architecture Summary

Per graphify (1434 nodes / 2830 edges / 83 communities), the 10 god nodes are
the core abstractions:

| God node | Degree | Role |
|---|---:|---|
| `MemoryService` | 124 | Top-level facade in `service.py` — ingest/query/run_cycle |
| `Claim` | 115 | Dataclass in `models.py` — the atomic unit of memory |
| `CitationInput` | 113 | Source attribution wrapper, required on every ingest |
| `QdrantBackend` | 64 | Vector search backend (optional, `[qdrant]` extra) |
| `PostgresStore` | 54 | Parity backend to SQLiteStore for multi-host deployments |
| `EntityGraph` | 51 | Typed-edge relationship graph on top of claim_links |
| `FeedbackTracker` | 51 | Records validator/steward outcomes for quality scoring |
| `OperatorQueue` | 45 | FIFO with atomic dequeue + crash recovery |
| `ClaimLink` | 44 | Typed relationships (14 types after v3.3.0) |
| `EmbeddingProvider` | 43 | 3-tier fallback: sentence-transformers → Gemini → hash-v1 |

The 83 communities group roughly into: **storage** (mixins + postgres),
**CLI** (cli + 2 handler modules), **wiki** (wiki_engine + vault_*),
**steward** (jobs/* + llm_steward), **operator** (stream runners + queue),
**dream bridge** (dream_* + transcript_miner), **entity registry**
(new in v3.3.0), **MCP server**, **dashboard** (HTTP read-only UI).

## Current State

**Recent commits** (last 5):
- `309110f` feat: entity registry + RESOLVER + typed relationships + graphify (v3.3.0)
- `ce4fd3c` fix: 5 NameError bugs + security filter consolidation + README stats
- `bacbbd8` chore: bump to 3.2.1 + CHANGELOG for packaging/docs fixes
- `12d126b` fix: relax 'quick' SLO thresholds to survive GHA runner variance
- `c457f78` fix: follow-up fixes from second code review

**Working**: 990 tests passing, 39 skipped, 0 failed. CI green 8/8 jobs.
PyPI publishing via OIDC trusted publisher. Entity registry + typed
relationships shipped.

**Known soft issues** (from recent MemoryMaster claims):
- 6 files still over 800 LOC (postgres_store, steward, operator, dashboard,
  mcp_server, llm_steward) — deferred to future refactor
- 13 modules at 0% test coverage (dream_bridge, verbatim_store, vault_linter,
  etc.) — identified by audit agent, not blocking
- Test coverage overall ~60.4%

## Key Files

- `memorymaster/service.py` — `MemoryService` facade (ingest/query/run_cycle)
- `memorymaster/storage.py` — `SQLiteStore(SchemaMixin, ReadMixin, WriteClaimsMixin, LifecycleMixin)`
- `memorymaster/mcp_server.py` — FastMCP stdio server, 22 MCP tools
- `memorymaster/cli.py` — argparse entry point, delegates to handlers
- `memorymaster/cli_handlers_basic.py` — claims/query/list/ops handlers
- `memorymaster/cli_handlers_curation.py` — wiki/snapshot/qdrant/dream handlers
- `memorymaster/wiki_engine.py` — absorb/cleanup/breakdown (Karpathy+Farza pattern)
- `memorymaster/entity_registry.py` — canonical entities + alias resolution (v3.3.0)
- `memorymaster/postgres_store.py` — Postgres backend parity (1655 LOC)
- `memorymaster/steward.py` — validator loop + proposal lifecycle (1627 LOC)

## Active Issues

- `test_run_stream_resumes_from_checkpoint_state` previously flaky — FIXED
  in v3.2.2 (was a real bug in `_seek_to_offset`)
- GHA runner variance up to 10x on identical commits — SLO thresholds
  relaxed in v3.2.1, not a code regression
- 6 oversized files awaiting mixin refactor (deferred to 3.4.0)
- 13 modules at 0% test coverage (tracked, not blocking release)

## Monitoring Signals

### Log Signals

```yaml
- id: python-tracebacks
  name: Python runtime tracebacks
  severity: P1
  type: log
  source: memorymaster.log  # only present when running dashboard or operator
  monitor_script: |
    # Works from Git Bash on Windows; source file path resolved from CWD.
    # Pipeline: tail -> pattern filter -> dedupe -> sanitize -> rate limit.
    tail -F memorymaster.log 2>/dev/null \
      | grep --line-buffered -E "Traceback|ERROR|CRITICAL" \
      | awk '!seen[$0]++ {print; fflush()}' \
      | sed -E 's/(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{36}|AIza[0-9A-Za-z_\-]{35}|Bearer [A-Za-z0-9._-]+)/[REDACTED]/g' \
      | awk 'NR - last_n > 0 {print; last_n=NR; fflush()}'
  trigger_pattern: "Traceback|UnhandledException|OperationalError"
  trigger_description: "Unhandled Python exception in long-running process (dashboard/operator)"
  action_level: read_only
  action_script: "escalate"
  cooldown_minutes: 5
  max_actions_per_hour: 6
  escalate_to: "telegram:2128295779"
```

### Health Check Signals

```yaml
- id: db-integrity
  name: SQLite DB integrity check
  severity: P0
  type: health_check
  source: memorymaster.db
  monitor_script: |
    sqlite3 memorymaster.db "PRAGMA integrity_check;" 2>&1
  trigger_pattern: "^(?!ok).*"
  trigger_description: "SQLite integrity check returned anything other than 'ok'"
  action_level: read_only
  action_script: "escalate"
  cooldown_minutes: 60
  max_actions_per_hour: 1
  escalate_to: "telegram:2128295779"
```

```yaml
- id: mcp-import
  name: MemoryMaster package importable
  severity: P1
  type: health_check
  source: "python -c 'import memorymaster; print(memorymaster.__version__)'"
  monitor_script: |
    python -c "import memorymaster; print(memorymaster.__version__)" 2>&1
  trigger_pattern: "Error|Traceback"
  trigger_description: "Package failed to import (broken install or Python env)"
  action_level: read_only
  action_script: "escalate"
  cooldown_minutes: 30
  max_actions_per_hour: 2
  escalate_to: "telegram:2128295779"
```

### Test Signals

```yaml
- id: test-suite
  name: Full pytest suite
  severity: P2
  type: test
  source: "python -m pytest tests/ -q --tb=line"
  monitor_script: |
    # Run once on demand, not on interval. OmniClaude invokes this when it
    # needs to know the current test state.
    python -m pytest tests/ -q --tb=no 2>&1 | tail -5
  trigger_pattern: "failed|error|interrupted"
  trigger_description: "Test suite has failures, errors, or was interrupted"
  action_level: fix_and_pr
  action_script: "escalate"
  cooldown_minutes: 120
  max_actions_per_hour: 1
  escalate_to: "telegram:2128295779"
```

### Build Signals

```yaml
- id: wheel-build
  name: Package builds cleanly
  severity: P2
  type: build
  source: "python -m build && twine check dist/*"
  monitor_script: |
    python -m build --wheel 2>&1 | tail -5 && twine check dist/*.whl 2>&1 | tail -3
  trigger_pattern: "FAILED|ERROR"
  trigger_description: "Wheel build or twine metadata check failed"
  action_level: read_only
  action_script: "escalate"
  cooldown_minutes: 240
  max_actions_per_hour: 1
  escalate_to: "telegram:2128295779"
```

### File Watcher Signals

```yaml
- id: db-size-drop
  name: Unexpected DB shrink
  severity: P0
  type: file_watcher
  source: memorymaster.db
  monitor_script: |
    # Alert if DB size drops more than 10% in 5 min — possible corruption
    # or accidental truncation. This is a polling signal, not a tail.
    CURR=$(stat -c '%s' memorymaster.db 2>/dev/null || stat -f '%z' memorymaster.db 2>/dev/null)
    echo "size=$CURR"
  trigger_pattern: "size=[0-9]+"
  trigger_description: "Watches absolute size. OmniClaude compares with previous tick and alerts on >10% drop."
  action_level: read_only
  action_script: "escalate"
  cooldown_minutes: 30
  max_actions_per_hour: 2
  escalate_to: "telegram:2128295779"
```

## Events This Project Emits

MemoryMaster itself does NOT currently write to `~/.omniclaude/inbox.jsonl`.
Future enhancement: the Stop hook could emit a `task_done` event whenever a
Claude Code session working on memorymaster completes a turn, so OmniClaude
knows when to ask "what's next?".

Recommended events to emit (M1+):
- `task_done` — after each successful commit or test green
- `needs_decision` — when Claude hits an ambiguous spec
- `ci_fail` — when `gh run list` shows a new failure
- `pypi_publish` — when a new tag triggers the publish workflow

## How to Verify It Works

1. `python -m memorymaster --db memorymaster.db query "test"` → returns JSON, no error
2. `python -m pytest tests/ -q --tb=line` → "990 passed"
3. `pip show memorymaster` → Version: 3.3.0
4. `sqlite3 memorymaster.db "PRAGMA integrity_check"` → `ok`
5. `gh run list --limit 1` → latest run `success`

## Dependencies on Other Projects

MemoryMaster is a **dependency of** (not depends on):

- **OmniClaude** itself — uses `mcp__memorymaster__query_memory` to look up
  past fixes before spawning
- **Every Claude Code session** via the recall hook — queries on every
  UserPromptSubmit
- **The classify hook** — uses MemoryMaster claim_types as the routing
  vocabulary (DECISION, GOTCHA, BUG, CONSTRAINT, ARCHITECTURE, ENVIRONMENT,
  REFERENCE)

MemoryMaster has NO runtime dependencies on other workspace projects.
Optional extras (`[postgres]`, `[qdrant]`, `[embeddings]`, `[gemini]`,
`[mcp]`, `[security]`) pull external packages, not workspace projects.
