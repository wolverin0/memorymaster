<!-- doc-head: MemoryMaster 4.8 governed memory, observations, profiles, workflow analytics, and operations -->
<!-- Covers: installation, governed recall, graph observations, compiled profile, Workflow Intelligence, scheduling, and safety. -->
<!-- Key terms: claims, citations, graph observations, workflow sidecar, compiled profile, Gemini, SQLite, MCP. -->
<!-- Read when: evaluating, installing, operating, or upgrading MemoryMaster. -->
<!-- Default: private local SQLite; observations and generated views never bypass claim governance. -->
<!-- /doc-head -->

# MemoryMaster

[![CI](https://github.com/wolverin0/memorymaster/actions/workflows/ci.yml/badge.svg)](https://github.com/wolverin0/memorymaster/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/memorymaster.svg)](https://pypi.org/project/memorymaster/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

MemoryMaster is persistent memory for coding agents where every durable fact is
a governed claim—not an opaque chunk that silently lives forever.

It captures evidence, extracts candidate claims, preserves citations, detects
conflicts, promotes trustworthy claims through a steward, and retires facts when
their support stops being current. Claude Code, Codex, Gemini-powered workers,
Hermes, and any MCP client can share the same local memory without surrendering
authority to a vector database or generated summary.

```text
evidence -> candidate claim -> steward -> confirmed claim -> governed recall
                                      \-> supported graph
                                           \-> candidate observation
                                                \-> steward -> opt-in recall
```

## What shipped in 4.7

| Capability | What it does | Default |
|---|---|---|
| Governed graph observations | Derives supported dependencies, constraints, recurring patterns and root causes from confirmed evidence | **Off by default** (`MEMORYMASTER_GRAPH_OBSERVATIONS=1`); recall opt-in |
| Compiled user profile | Builds a disposable, cited projection from multiple independent sessions | **Off by default** (`MEMORYMASTER_COMPILED_PROFILE=1`); never an instruction source |
| Fast conversational recall | Finds relevant claims on the local lexical path without an embedding, Qdrant or provider call | On |
| Private-context intake guard | Redacts RFC1918 topology and absolute Windows/UNC paths from durable claim fields while preserving useful prose | On |
| Governed skills | Stores reviewed reusable procedures separately from ordinary facts | Recall opt-in |
| Hermes integration | Provides exact session/project scoping, local HTTP/stdio compatibility and replay-safe outbox behavior | Optional integration |
| Operational review | Performs a read-only six-hour integrity, queue, profile, intake and retrieval review | Optional Windows task |
| Workflow Intelligence | Mines Claude/Codex trajectories into local redacted analytics and inert improvement candidates | On demand; separate sidecar, no automatic promotion |

MemoryMaster 4.8 uses the configured **Gemini extraction + Gemini consolidation**
path for this installation; consolidation moved off GLM on 2026-08-20 when that
plan was retired, and now runs through the Antigravity (`agy`) CLI over OAuth.
OpenAI and Anthropic remain optional provider adapters; neither is required for
graph-observation discovery or ordinary recall.

## Why MemoryMaster exists

Typical memory stacks optimize retrieval while leaving correctness and retirement
to the caller. MemoryMaster makes those properties explicit:

- Every claim has lifecycle state, scope, provenance, confidence and validity.
- Trusted recall returns confirmed, authorized, non-sensitive claims only.
- Contradictions become visible conflicts instead of silently coexisting.
- Citations and support tables make derived output traceable back to evidence.
- `forget` previews logical retirement; it never implies an unsafe hard delete.
- SQLite is authoritative. Qdrant is optional and may only propose IDs that are
  rehydrated and re-authorized from SQLite.
- Generated observations, profiles, skills and wiki pages never recursively
  reinforce the claims that produced them.

## Quick start

Install the private local MCP profile:

```powershell
python -m pip install "memorymaster[mcp,capture,security]"
memorymaster-setup --yes --profile minimal --no-full-stack --json
```

Restart the agent session once so its long-lived MCP process loads the installed
package, then verify:

```text
query_memory("What decisions have we made about storage?")
```

Try the complete lifecycle in a disposable database:

```powershell
memorymaster --json demo
```

## Four public operations

The stable Python, CLI and MCP interface is intentionally small:

```powershell
memorymaster --workspace . remember --text "Atlas uses SQLite WAL."
memorymaster --workspace . recall "What does Atlas use?"
memorymaster --workspace . forget --source-item-id 1
memorymaster --workspace . improve --scope project:atlas
```

```python
from memorymaster import forget, improve, recall, remember

receipt = remember(text="Atlas uses SQLite WAL.", scope="project:atlas")
context = recall("What does Atlas use?", scope_allowlist=["project:atlas"])
preview = forget(source_item_id=receipt.source_item["id"])
queued = improve(scope="project:atlas")
```

- `remember` stores source/evidence lineage and queues governed extraction.
- `recall` is confirmed-only in trusted mode.
- `forget` previews by default; `--apply` performs lifecycle retirement.
- `improve` queues bounded work. It does not confirm or rewrite claims inside
  the request.

See [Public v1](docs/public-v1.md) for receipts and full parameter contracts.

## Workflow Intelligence

The optional `workflow` command family audits retained Claude Code, Codex, and
Wezbridge trajectories in a rebuildable local sidecar. It does not write claims,
modify agent instructions, or treat an LLM judgment as proof of success.

```powershell
memorymaster workflow scan --deep human
memorymaster workflow report
memorymaster workflow candidates
```

LLM classification is a separate opt-in command. The provider-neutral
completion receipt hook ships unregistered and defaults to `off`; advisory mode
cannot be configured until its 14-day shadow evidence gate passes. See
[Workflow Intelligence](docs/workflow-intelligence.md) for the schema, commands,
redaction boundary, recurrence rules, and rollout gate.

## Graph observations (PPR-7)

> **Off by default.** Enable with `MEMORYMASTER_GRAPH_OBSERVATIONS=1`.
> The claims database works fully without it; this is an optional layer on top.

Graph observations answer questions that individual facts cannot, such as:

- Which three blockers form one dependency chain?
- What recurring pattern is supported by several independent episodes?
- Which root cause is cited by multiple confirmed claims?
- Which observation became stale when its supporting evidence was retired?

Component membership is deterministic. Exact canonical graph signatures and
union-find decide which evidence belongs together; an LLM may summarize an
eligible component but cannot choose membership, invent support, promote its own
output, or feed the result back into graph extraction.

Recall remains explicit:

```powershell
memorymaster --workspace . recall "What dependencies keep recurring?" --include-observations
```

```python
result = recall(
    "What dependencies keep recurring?",
    include_observations=True,
    observation_limit=2,
)
```

Trusted mode revalidates support at read time and returns confirmed observations
only. Exploratory mode can label candidates or stale observations. Ordinary
recall remains unchanged while `include_observations` is off.

## Evidence-bound compiled profile

> **Off by default.** Enable with `MEMORYMASTER_COMPILED_PROFILE=1`.
> Recall does not depend on it; when disabled, nothing is injected at session start.

The compiled profile turns repeated, independently supported transcript facts
into a bounded session-start briefing. It is a disposable projection:

- SQLite transcript/support rows remain authoritative.
- New or replacement facts need at least two independent sessions.
- Unknown support IDs, sensitive output, instructions and malformed provider
  output fail closed.
- The injected profile is context about the user—not permission and not an
  instruction hierarchy.

Enable both 4.7 features for new processes:

```powershell
[Environment]::SetEnvironmentVariable("MEMORYMASTER_GRAPH_OBSERVATIONS", "1", "User")
[Environment]::SetEnvironmentVariable("MEMORYMASTER_COMPILED_PROFILE", "1", "User")
```

Hooks are re-read on every event and update immediately. MCP servers and other
long-lived daemons must be restarted to load newly installed package code or new
environment variables.

## Scheduled operation—release first, review afterward

There is **no 24-hour implementation or release prerequisite**. A long observation
window is useful only as later evidence about queue health, cost and lifecycle.
It must never keep working code out of `main` merely because the clock has not
elapsed.

On Windows, install the bounded read-only review task after installing the
release:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install-windows-operational-review.ps1 `
  -PythonExe "<runtime-python.exe>" `
  -Database "<memorymaster.db>" `
  -ExpectedVersion "4.7.2" `
  -EveryHours 6
```

`MemoryMaster-Operational-Review` then checks:

- installed version, schema, SQLite `quick_check` and foreign keys;
- observation backlog, blocked jobs and expired leases;
- compiled-profile runs, facts and exact support counts;
- recent claim fields for private topology or absolute-path residue;
- the configured natural-language retrieval canary.

It writes `latest.json` and append-only execution history under the user's local
application-data directory. The review opens SQLite read-only, performs no claim
or job mutation, and never turns task transport into a false success receipt.
Exit codes are `0=PASS`, `1=FAIL`, and `3=WARN`.

## Architecture and governance

```text
producer
  -> source_items
  -> evidence_items
  -> candidate claims
  -> steward proposals / lifecycle events
  -> confirmed claims
  -> FTS5 recall + supported entity graph
  -> optional observations / skills / compiled profile
```

The authoritative product is one private local SQLite database in WAL mode plus
a stdio MCP server. Optional Workflow Intelligence uses a disposable analytics
sidecar that cannot participate in recall or lifecycle authority. No cloud
service or vector server is required.
PostgreSQL team operation remains explicitly deferred; Qdrant remains an
optional semantic accelerator rather than a source of truth.

Important boundaries:

- Claim fields pass through the shared sensitivity filter on every ingest path.
- Raw user-selected source/evidence remains governed by the separate preservation
  boundary described in [ADR-0006](docs/adr/0006-sensitivity-filter-boundary.md).
- Generated observations cannot support future observations.
- Trusted graph traversal requires active, authorized support in the same scope
  and tenant.
- Schema changes use immutable checksum-verified migrations.
- The Obsidian wiki is an opt-in human view, not the read layer.

See [Architecture](docs/architecture.md), [Operations](docs/operations.md), and
the [Graph Observations ledger](.planning/GRAPH-OBSERVATIONS-V1.md).

## MCP configuration

```json
{
  "mcpServers": {
    "memorymaster": {
      "command": "memorymaster-mcp",
      "env": {
        "MEMORYMASTER_DEFAULT_DB": "<path-to-memorymaster.db>",
        "MEMORYMASTER_WORKSPACE": "<project-root>",
        "MEMORYMASTER_MCP_AUTH_MODE": "local-trusted"
      }
    }
  }
}
```

`local-trusted` is only for a private SQLite stdio process controlled by one OS
user. Regenerate old brownfield entries that do not declare an authorization
mode. The full tool inventory is generated from code in
[release truth](docs/generated/release-truth.md); operational examples are in
[MCP tools](docs/MCP-TOOLS.md).

## Providers

Provider calls are for extraction, consolidation and selected steward phases—
not for ordinary local recall.

| Provider | Configuration | Typical use |
|---|---|---|
| Gemini | `MEMORYMASTER_LLM_PROVIDER=google` plus configured Google credentials | Activated extraction path |
| Gemini through the Antigravity `agy` CLI | OAuth session cached by a prior interactive `agy` run | Activated Dreaming/profile consolidation path |
| GLM through authenticated OpenCode | `MEMORYMASTER_DREAM_CONSOLIDATE_PROVIDER=glm` | Retired 2026-08-20; deselected, not deleted |
| Claude CLI OAuth | `MEMORYMASTER_LLM_PROVIDER=claude_cli` | Optional batch/steward path |
| Ollama | `MEMORYMASTER_LLM_PROVIDER=ollama` | Optional local provider |
| OpenAI / Anthropic APIs | corresponding provider and environment credential | Supported optional adapters |

Never put credentials in claims, repository files, task arguments, logs or
generated profiles.

## Useful commands

```powershell
# Trusted recall with score explanation
memorymaster query "topic" --explain

# One bounded steward cycle
python -m memorymaster --db memorymaster.db run-cycle

# Database checks
memorymaster --db memorymaster.db --json integrity --quick-check --fk-check

# Dashboard
memorymaster-dashboard --db memorymaster.db

# Validate generated release inventory
python scripts/generate_release_truth.py --check
```

## Upgrade and rollback

Upgrade the package, then restart long-lived MCP/daemon processes:

```powershell
python -m pip install --upgrade "memorymaster[mcp,capture,security]"
```

Feature rollback does not delete history:

```powershell
[Environment]::SetEnvironmentVariable("MEMORYMASTER_GRAPH_OBSERVATIONS", "0", "User")
[Environment]::SetEnvironmentVariable("MEMORYMASTER_COMPILED_PROFILE", "0", "User")
```

Disabling generation/recall leaves additive tables and audit history intact.
Candidates can be archived and confirmed generated observations made stale only
through the governed lifecycle—not by deleting database rows.

## Project status and documentation

- Stable release history: [CHANGELOG](CHANGELOG.md)
- Canonical documentation verdicts: [DOCS-MAP](DOCS-MAP.md)
- Install and agent wiring: [AGENT-INSTALL](docs/AGENT-INSTALL.md)
- Public API: [Public v1](docs/public-v1.md)
- Operational procedures: [Operations](docs/operations.md)
- Security and supply chain: [Security](docs/security_supply_chain.md)
- Full feature handbook: [Handbook](docs/handbook.md)

MemoryMaster is MIT licensed. Contributions should preserve SQLite authority,
claim governance, exact support lineage, privacy boundaries and opt-in derived
recall.
