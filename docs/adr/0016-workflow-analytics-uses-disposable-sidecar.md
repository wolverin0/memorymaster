<!-- doc-head: accepted boundary for the disposable Workflow Intelligence sidecar -->
# 0016 Workflow analytics uses a disposable sidecar
# Covers: why trajectory analytics is separate from authoritative MemoryMaster data.
# Key terms: sidecar, rebuildable analytics, authority, provenance, workflow intelligence.
# Read when: changing workflow storage, retention, reports, or the single-database contract.
# Decision: claims stay in memorymaster.db; transcript analytics use a disposable local SQLite sidecar.
<!-- /doc-head -->

Date: 2026-08-30

Status: Accepted for implementation; feature remains off/unregistered by default.

## Context

ADR-0001 requires all authoritative MemoryMaster tables to share one logical
database so claims, citations, evidence, and lifecycle state cannot silently
diverge. Coding-agent trajectory analysis has a different lifecycle: it is
derived from retained provider logs, can be rebuilt, contains high-volume
operational metadata, and must never become trusted recall merely because it
was observed.

Putting raw or normalized trajectories into `memorymaster.db` would blur the
authority boundary, enlarge backups and migrations, and make deleting derived
analytics look like deleting memory. A cloud trace platform or vector store is
not justified for a local-first first version.

## Decision

Workflow Intelligence uses a separate WAL-mode SQLite database at
`~/.memorymaster/workflow-intelligence.db` by default. The path is configurable.
The sidecar contains source census, session/turn/action normalization, redacted
evidence excerpts, deterministic outcome signals, candidates, human analytics
reviews, and content-free hook receipts.

The sidecar is non-authoritative and rebuildable. It cannot supply ordinary
recall, confirm claims, approve skills, edit instructions, or activate hooks.
Only an explicit human-governed export may carry an inert proposal toward an
existing MemoryMaster promotion surface.

Main-database migration 0024 is intentionally different: it stores the minimal
hashed root-session lineage required to enforce recurrence on governed rule and
skill candidates. That lineage is authoritative governance state and therefore
belongs in `memorymaster.db` with SQLite/Postgres parity.

## Consequences

- ADR-0001 remains authoritative for MemoryMaster claims and governed data.
- The default product still needs no server, vector database, or cloud platform.
- Sidecar loss affects analytics and review labels, not memory correctness.
- Reports must hide local paths and limit/redact excerpts.
- LLM classification stays explicit and advisory; deterministic evidence wins.
- Hook activation and policy promotion require separate human decisions.
- Cross-database joins are forbidden in correctness-critical runtime paths.

