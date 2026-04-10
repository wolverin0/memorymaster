# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.2.0] - 2026-04-09

### Added

- **Wiki frontmatter schema**: Every absorbed article in `obsidian-vault/wiki/**/*.md` now carries `description` (~150 char), `tags`, and `date` fields for progressive disclosure. Helpers `_extract_description`, `_build_tags`, `_yaml_escape` in `wiki_engine.py`.
- **Obsidian Bases generator** (`vault_bases.py`): Auto-generates 5 dynamic dashboards (`all-claims.base`, `gotchas.base`, `decisions.base`, `recent.base`, `needs-review.base`) under `obsidian-vault/bases/`. New `bases-generate` CLI command. `wiki-absorb` regenerates Bases automatically (suppress with `--no-bases`).
- **Classify hook** (`config-templates/hooks/memorymaster-classify.py`): Regex signal matcher for UserPromptSubmit with 7 signals (DECISION, BUG_ROOT_CAUSE, GOTCHA, CONSTRAINT, ARCHITECTURE, ENVIRONMENT, REFERENCE) in Spanish + English. Latin-letter lookarounds make it CJK-safe. Zero LLM, ~5 ms runtime.
- **Validate-wiki hook** (`config-templates/hooks/memorymaster-validate-wiki.py`): PostToolUse Edit/Write hook scoped to `obsidian-vault/wiki/**/*.md`. Checks frontmatter completeness and warns on orphan articles (no `[[wikilinks]]` and body > 300 chars).
- **SessionStart hook** (`config-templates/hooks/memorymaster-session-start.py`): Injects recent claims, last cycle summary (ingest/validate/decay/supersession counts), pending candidates, and recently updated wiki articles at session start. Scope auto-derived from cwd.
- **PyPI publish workflow** (`.github/workflows/publish.yml`): Auto-publishes on `git tag v*.*.*` push using PyPI Trusted Publisher with OIDC (no API tokens in secrets).
- **32 E2E tests** (`tests/test_obsidian_mind_patterns.py`) covering all 5 obsidian-mind-inspired components.
- **`benchmarks/README.md`**: Download instructions for the LongMemEval oracle dataset (~15 MB).
- **CLI command**: `bases-generate --output <vault>` regenerates Obsidian Bases on demand.
- **`setup-hooks.py` updates**: Now installs the 3 new hooks alongside the legacy recall + auto-ingest pair.

### Fixed

- **`_seek_to_offset` returned `start_offset = 0` always**: When `MemoryOperator._run_stream_json` resumed from a saved offset, the seek succeeded but the function still returned `(0, read_offset)`, breaking checkpoint resumption. Now returns `(read_offset, read_offset)` on success and `(0, 0)` on error. Fixes `test_run_stream_resumes_from_checkpoint_state` (was the last known flaky test).
- **`test_returns_valid_sha`**: GitHub Actions runners have no global git identity, so `git commit --allow-empty` failed with exit 128. Test now sets `user.email`/`user.name` locally in the temp dir before the commit.
- **`test_semantic_model_calls_transformer`**: Used `import numpy` unconditionally despite numpy not being a base dependency. Now uses `pytest.importorskip("numpy")` so the test skips gracefully when numpy is unavailable.
- **CI: 3 tests failing for 5 consecutive runs** — all 3 fixed above. CI is now green again.

### Changed

- **CLAUDE.md (global)**: Documented SessionStart, classify, and validate-wiki hooks under "How memory flows automatically" so future Claude sessions know to trust the routing hints and react to wiki hygiene warnings.
- **AGENTS.md**: Added wiki frontmatter schema enforcement to Boundaries section.
- **README.md**: Added 3 new entries to Key Features (LLM Wiki, Obsidian Bases, 7-Hook Stack) and a new "New in v3.2" section documenting all the obsidian-mind-inspired patterns.

### Removed

- **Repo cruft from root**: Deleted `entity_extraction.log`, `qdrant_sync.log`, `qdrant_sync_result.json`, `test_output.txt` from the working tree and added them to `.gitignore`.
- **`benchmarks/longmemeval_oracle.json` (~15 MB)** removed from tracking and added to `.gitignore` — it is a public dataset and should be downloaded with the documented commands instead of bloating the repo.

## [3.1.0] - 2026-04-08 (never published to PyPI)

### Added

- **LLM Wiki architecture**: Compiled truth + append-only timeline articles, Karpathy/Farza style. New modules `wiki_engine.py`, `vault_linter.py`, `vault_log.py`, `vault_synthesis.py`, `vault_query_capture.py`.
- **CLI commands**: `wiki-absorb`, `wiki-cleanup`, `wiki-breakdown`, `lint-vault`, `mine-transcript`, `verify-claims`.
- **Verify-claims**: Cross-checks claims that mention file paths or symbols against the actual codebase using `ripgrep`, sub-100 ms per check.
- **MemPalace-inspired upgrades**: Block-based Stop hook with `decision: block` checkpoint every N human messages, PreCompact hook, content-hash dedup (`hash-<sha256>` idempotency keys), bi-temporal `valid_from`/`valid_until` fields on claims, transcript miner.
- **Multi-provider LLM client** (`llm_provider.py`): Google / OpenAI / Anthropic / Ollama with key rotation.
- **Setup script** (`scripts/setup-hooks.py`): Interactive installer for hooks, MCP, env vars, and steward cron.
- **Config templates** (`config-templates/`): Hook templates with `__MEMORYMASTER_PROJECT_ROOT__` placeholder and CLAUDE.md / AGENTS.md append snippets.

### Fixed

- **WAL mode mandatory**: `PRAGMA journal_mode = WAL` now enforced on every connection to prevent DB corruption from concurrent writes (caused by OpenClaw `scp` overwriting an open DB).
- **Hardcoded path in `claim_verifier.py`**: Replaced with dynamic project root detection.
- **35+ silent `except: pass` blocks** in `llm_provider.py`: Now log the exception so API failures are visible instead of returning empty results.
- **Dream-bridge cross-project pollution**: Added scope filter so dream-seed only exports claims from the current project.
- **Hardened sensitivity filter**: Added regexes for Telegram bot tokens, Stripe keys, Supabase keys, and SSH commands.
- **MCP `ingest_claim`**: Auto-generates `CitationInput(source="mcp-session")` when caller does not provide one (was rejecting otherwise-valid ingests with "At least one citation required").
- **Timezone-aware vs naive datetime crash** in `decay.py::_parse_iso`.

## [3.0.0] - 2026-04-05 (never published to PyPI)

### Added

- **Verbatim memory layer** (`verbatim_store.py`): Raw conversation storage table with FTS5 search and Qdrant vector search using OpenAI text-embedding-3-small (1536 dims, Cosine).
- **LongMemEval benchmarks**: `benchmarks/longmemeval_runner.py` (FTS5 baseline, scored 5.6%) and `benchmarks/longmemeval_vector_runner.py` (Qdrant vector, scored 25% on 20 questions). Reference: MemPalace ChromaDB scores 96.6%.
- **Curate-vault command**: LLM-organized Obsidian export with topic clustering and wikilinks (later deprecated by `wiki-absorb`).

## [2.0.0] - 2026-03-08

### Added

- **Centralized Config** (`config.py`): Frozen `Config` dataclass with 11 env vars + JSON config file support. All hardcoded weights replaced with configurable values.
- **Context Optimizer** (`context_optimizer.py`): `query_for_context(budget=4000)` with greedy knapsack packing and 3 output formats (text/xml/json). New `query_for_context` MCP tool (13 total).
- **Conflict Resolution** (`conflict_resolver.py`): 5-tier auto-resolution (pinned > confidence > recency > citations > id), `contradicts` links, and `policy_decision` audit events.
- **Deduplication** (`jobs/dedup.py`): Two-gate detection (cosine similarity + text overlap), chain prevention, `supersedes` links, summary events.
- **Staleness Detection** (`jobs/staleness.py`): File watcher with `mtime` and `git` modes, citation-based path extraction, pinned claim exclusion.
- **LLM Compaction** (`jobs/compact_summaries.py`): Embedding-based clustering with LLM summarization, `derived_from` links, confirmed summary claims.
- **Git Versioning** (`snapshot.py`): SQLite `.backup()` API snapshots, rollback with safety backup, field-level diff, post-commit hook installer.
- **Claim Graph**: `claim_links` table with 5 typed relationships (`supersedes`, `contradicts`, `supports`, `derived_from`, `relates_to`).
- **Hierarchical IDs**: `mm-{4hex}.{n}.{n}` human-readable IDs derived from `derived_from` links, accepted in all CLI commands.
- **Multi-tenancy**: Row-level `tenant_id` isolation at service layer with `_check_tenant_access()` enforcement.
- **Connection Retry** (`retry.py`): Exponential backoff wrapper for SQLite and Postgres connections.
- **Operator Queue** (`operator_queue.py`): SQLite WAL-backed FIFO with atomic dequeue and crash recovery.
- **Key Rotation**: Round-robin API key selection with per-key cooldown tracking on 429 errors.
- **Auto-validate Pipeline**: Chained extraction + deterministic validation after LLM claim extraction.
- **FTS5 Search**: Content-synced FTS5 virtual table with BM25 ranking and proper query escaping.
- **Semantic Embeddings**: 3-tier fallback (sentence-transformers MiniLM-L6-v2, Gemini API, hash-v1) with `is_semantic` weight switching.
- **JSON Output**: Global `--json` flag for all CLI commands with structured envelope format.
- **Stealth Mode**: `--stealth` flag for local-only experimentation with auto-detection.
- **New CLI Commands**: `context`, `dedup`, `resolve-conflicts`, `ready`, `history`, `link`/`unlink`/`links`, `check-staleness`, `compact-summaries`, `snapshot`/`snapshots`/`rollback`/`diff`, `install-hook`, `stealth-status`.
- **Postgres Parity**: 32/32 public method parity with SQLite store including claim links, human IDs, and tenant filtering.
- **380+ tests** across 40+ test modules (up from 82 tests in v1.0.0).

### Fixed

- Dashboard test assertions updated to match actual HTML output (`">Claims<"` instead of `"Claims Table"`).
- Steward `_get_git_head()` hardened with timeout, path resolution, and 40-hex output validation.
- Scheduler `get_git_head()` hardened with same protections.
- `_is_valid_url()` now validates hostname via IP address or regex (was accepting malformed URLs).
- Decay module now uses `DECAY_BY_VOLATILITY` constant instead of missing reference.
- Bearer token redaction pattern lowered minimum from 20 to 8 chars to catch short tokens.
- Added JWT, GitHub token, hex token, markdown credential, inline credential, and connection string redaction patterns.

### Changed

- Version bump from 1.1.0 to 2.0.0 (major: new public API surface, multi-tenancy, claim graph).
- Retrieval weights switch automatically based on `is_semantic` embedding provider.
- All hardcoded weights across 5 modules replaced with `get_config()` lookups.
- Service layer now uses `create_best_provider()` for automatic embedding tier selection.
- Added `embeddings` and `gemini` optional dependency groups to `pyproject.toml`.

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
