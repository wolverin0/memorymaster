# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-03-07

### Added

- **Core Engine**: 6-state claim lifecycle (`candidate` -> `confirmed` -> `stale` -> `superseded` -> `conflicted` -> `archived`) with append-only event log and citation tracking.
- **Structured Claims**: Subject-predicate-object triples with confidence scores, volatility tags, and scope isolation.
- **Hybrid Retrieval**: Lexical + vector + freshness + confidence ranking with progressive tiered fallback.
- **Steward Governance**: Filesystem grep, deterministic format, citation locator, semantic probe, and tool probe validators with human-in-the-loop proposal/approve/reject workflow.
- **Operator Runtime**: JSONL inbox streaming with restart-safe checkpointing, durable pending-turn queue, progressive retrieval, and configurable maintenance cadence.
- **MCP Server**: 12 tools for Claude Code / Codex integration (`init_db`, `ingest_claim`, `run_cycle`, `query_memory`, `list_claims`, `list_events`, `pin_claim`, `compact_memory`, `run_steward`, `list_steward_proposals`, `resolve_steward_proposal`, `open_dashboard`).
- **Dashboard**: Real-time HTML dashboard with claims table, timeline feed, conflict comparisons, review queue, and SSE operator stream.
- **Connectors**: Import from Git commits, tickets, Slack, email (IMAP), Jira, GitHub, and generic OpenAI/Claude/Gemini conversation exports.
- **Security**: Auto-redaction of tokens/keys/passwords at ingest, policy-gated sensitive access, Fernet encryption for raw payloads, and non-destructive `redact-claim` with audit trail.
- **Dual Backend**: Full SQLite and Postgres (with optional pgvector) parity.
- **Performance**: SLO-driven benchmarks with configurable profiles (`quick`, `sustained`, `production`), p95 latency gates, throughput floors, and zero-miss quality checks.
- **Incident Drills**: Automated drill runner with perf + eval + operator E2E + integrity reconciliation + compaction traceability + HMAC-signed signoff artifacts.
- **Metrics Export**: Prometheus text format and structured JSON metrics from operator event logs.
- **Review Queue**: Priority-ranked triage of stale/conflicted claims with dashboard approve/reject actions.
- **Compaction**: Citation-preserving history summarization with traceability graph artifacts.
- **82 tests passing** across 21 test modules covering core, steward, operator, dashboard, connectors, and performance.

### Fixed

- SSE stream newline encoding (was sending literal `\n` instead of actual newlines).
- Operator JSON decode error handling (was blocking queue permanently instead of skipping bad entries).
- Operator event naming (`json_error` consistent with dashboard SSE listener).
- Review queue sensitive claim filtering (now properly passes `allow_sensitive` through to `list_claims`).
- Python 3.12 compatibility for `@dataclass(slots=True)` with `importlib.util` module loading.
- Steward test helpers now bypass SQLite uniqueness guards correctly.

## [0.1.0] - 2026-02-15

### Added

- Initial prototype with SQLite backend, basic ingest/query/cycle, and CLI.
