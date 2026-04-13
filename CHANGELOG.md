# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.4.0] - 2026-04-13

### Added

- **Bidirectional claim↔wiki binding**: new `claims.wiki_article` column + index stamps the slug of the wiki article each claim was absorbed into. Closes the one-way link that existed before — wiki frontmatter listed `claims: [ids]` but claims couldn't point back. `wiki_engine.absorb()` now writes both directions in the same pass via the new `_stamp_wiki_binding(db_path, claim_ids, slug)` helper.
- **Recall hook shows the wiki pointer**: `context_hook.recall()` appends `(compiled in [[<slug>]])` next to any claim that has a `wiki_article` stamp, so agents see not just the fact but where its compiled-truth version lives. Inspired by Marcosomma's "Memory Bundle" pattern (binding > recall).
- **New CLI `wiki-backfill-bindings`**: one-shot migration that reads `claims: [ids]` frontmatter from every `<wiki_dir>/**/*.md` and stamps each listed claim with the file's slug. Run once after upgrading to v3.4 to backfill existing vaults.
- **`Claim.wiki_article` field** on the dataclass (default `None`) + readers on both SQLite (`_row_to_claim`) and Postgres (`PostgresStore._row_to_claim`).
- **Tests**: 8 new tests in `tests/test_wiki_binding.py` — schema shape, index presence, idempotent migration, stamp helper, silent no-op on empty input, dataclass roundtrip, recall formatter, backfill handler. Suite is 998 passed / 39 skipped.
- **LLM provider A/B benchmark harness** (`scripts/llm_benchmark.py`): 2-arm comparison (Gemini Flash Lite vs Ollama Gemma 4 e4b with thinking) on real session transcripts. Mirrors the auto-ingest curator prompt. Not part of the runtime; used to validate LLM choices before swaps.

### Changed

- Schema files `schema.sql` and `schema_postgres.sql` include `wiki_article TEXT` on the base `claims` DDL for fresh installs. Existing DBs get the column via the idempotent migration (`_ensure_binding_columns` on SQLite, `_ensure_binding_schema` on Postgres).

### Notes

- The feature is additive and the column is nullable. Pre-v3.4 DBs continue to work; `wiki_article` stays `NULL` until the next `wiki-absorb` run (or until `wiki-backfill-bindings` is invoked).
- Decision trail: benchmark of Gemini Flash Lite vs Gemma 4 e4b (8 sessions) showed Flash Lite extracts 3 claims/session vs Gemma 1 claim/session at the same warm latency (~2.7s). Auto-ingest hook stays on Flash Lite; Gemma remains a candidate for single-output batch tasks (conflict resolver, RESOLVER fallback, wiki-cleanup). `gemma-4-31b-it` via Gemini API (free tier, ~1500/day) works but latency is 10x Flash Lite and thinking can't be disabled — viable only for non-interactive batch.

## [3.3.1] - 2026-04-11

### Fixed

- **Scope hash-suffix bug in `_project_scope()` (mcp_server.py)**: The MCP server was appending a truncated SHA1 digest of the workspace path to every project scope (`project:wezbridge:a6a83c6a`). CLI ingests wrote `project:wezbridge` without the hash, and the two scopes never merged — sessions querying `project:wezbridge` missed claims stored in `project:wezbridge:a6a83c6a` and vice-versa. Fix: default to the canonical `project:<slug>` form. The hash-suffix escape hatch is preserved behind `MEMORYMASTER_SCOPE_DISAMBIGUATE=1` for hosts that genuinely have two workspaces with the same slug. Existing claims with hash suffixes were migrated to the canonical scope (341 claims across 6 scopes).
- **Claim type case inconsistency**: `service.ingest()` now normalizes `claim_type` to lowercase so that routing hints like `DECISION` from the classify hook don't create a duplicate type next to `decision`. 30 existing claims with ALL-CAPS types (GOTCHA, CONSTRAINT, ARCHITECTURE, BUG_ROOT_CAUSE, DECISION, REFERENCE) were normalized.
- **Orphan conflicted claims**: 6 claims had status `conflicted` but their canonical sibling already existed with status `confirmed`. The auto-resolver had skipped them because the confirmed sibling already "won" without competition. They were re-labeled `superseded` with `replaced_by_claim_id` pointing to the winning sibling. Total conflict count: 0.
- **Stale candidates**: Ran a full steward cycle — 200 claims decayed, 195 moved to stale, candidates older than 24h processed.

### Changed

- `project:*:<8hex>` scopes are now migration-compatible — on a fresh DB the scope has no hash, on an old DB the hash is preserved unless a migration strips it. This is the second time this bug has surfaced (first was in v3.2.2 with the tenant_id leak); documented as a constraint claim for future sessions.

## [3.3.0] - 2026-04-10

### Added

- **Entity Registry** (`entity_registry.py`): Canonical entities with alias resolution, inspired by GBrain. New tables `entities` and `entity_aliases` provide identity resolution so "MemoryMaster", "memorymaster", "MEMORYMASTER" all resolve to the same entity. `claim.entity_id` FK links claims to canonical entities. Auto-resolved on ingest via `resolve_or_create()`. CLI commands: `entity-list`, `entity-merge`, `entity-aliases`, `entity-backfill`. 684 existing subjects backfilled in 23ms.
- **RESOLVER.md**: MECE decision tree for wiki article routing (`obsidian-vault/wiki/RESOLVER.md`). 10 canonical types (bug, gotcha, decision, constraint, architecture, environment, reference, entity, pattern, fact) with disambiguation rules. Agents must read this before creating wiki content. Maps directly to the classify hook's routing hints.
- **9 new relationship types**: `implements`, `configures`, `depends_on`, `deployed_on`, `owned_by`, `tested_by`, `documents`, `blocks`, `enables` — expanding `CLAIM_LINK_TYPES` from 5 to 14. Schema migration recreates `claim_links` table with expanded CHECK constraint while preserving existing data. Enables domain-specific graph traversals like "what depends on Qdrant?"
- **`traverse_relationships()`**: BFS graph traversal on claim_links. Accepts `link_types` filter, `max_depth`, and `direction` (outgoing/incoming/both). Returns claims with depth, path, and link_type. Turns the flat claims DB into a queryable knowledge graph.
- **graphify integration**: `pip install graphifyy` + `graphify install` adds the graphify skill to Claude Code for building knowledge graphs from any folder. Not integrated into MemoryMaster codebase — used as a complementary standalone tool.

## [3.2.2] - 2026-04-10

### Fixed

- **5 NameError bugs from cli refactor**: `_score_str_from_payload`, `CitationInput`, `_SCORE_KEYS`, `print_claim` were referenced but not imported in the split handler files. All 5 cause NameError on `history`, `extract-claims --ingest`, `federated-query` CLI commands. Regression tests added for all 4 broken handlers.
- **TypeError on `ghost-notes --json`**: `_handle_ghost_notes` called `_json_envelope()` without the required `query_ms` kwarg.
- **UTF-8 BOM in `metrics_exporter.py`**: broke radon and mypy. Stripped.
- **test_stealth_mode collection error**: `STEALTH_DB_NAME` was auto-removed by ruff F401 fix from cli.py but tests imported it from there. Re-exported with `# noqa: F401` annotation.
- **Sensitivity filter: private IPs removed from canonical ingest filter**: `private_ipv4` pattern was incorrectly blocking legitimate infrastructure claims (e.g. "Server IP is 10.0.0.1"). Private IPs are now only filtered at export time (dream_bridge `_DREAM_EXTRA_PATTERNS`), not at ingest time.

### Added

- **Sensitivity filter extended**: 6 new patterns in `security.py` — Google API keys (`AIza*`), AWS STS keys (`ASIA*`), Slack tokens (`xoxb/xoxp/xoxa`), extended GitHub tokens (`ghu_/ghs_/ghr_`), Telegram bot tokens, DB connection URLs with embedded passwords (`postgres://user:pass@host`). All patterns tested with 20 new security test cases.
- **Sensitivity filter consolidated**: Deleted 4 duplicated regex blocks in `mcp_server.py`, `dream_bridge.py`, `transcript_miner.py`, `verbatim_store.py`. All now call `memorymaster.security.redact_text()` as single source of truth. New public API: `memorymaster.security.redact_text(text) -> (redacted, findings)`.
- **7 regression tests** (`test_handler_regressions.py`) covering all 4 handlers that had F821/TypeError bugs.
- **`autoresearch_daemon.py`**: `git_commit` and `git_revert` now use `run_argv()` (list form, `shell=False`) instead of f-string interpolation into `run()` (`shell=True`), removing a potential command injection footgun.

### Changed

- **130 unused imports cleaned** across 10 files after the cli/storage refactor (ruff F401 autofix).
- **README stats updated**: 22 MCP tools (was 21, `search_verbatim` was undocumented), 64 CLI commands (was "54+"), 1034 tests across 68 modules (was "932 across 66").

## [3.2.1] - 2026-04-10

### Added

- **`memorymaster-setup` entry point**: New `[project.scripts]` entry so pip-installed users can run the interactive installer via `memorymaster-setup` without needing the repo cloned. `scripts/setup-hooks.py` is now a 3-line shim that calls `memorymaster.setup_hooks:main` for backward compat with clone-based workflows.
- **`memorymaster-precompact.py` hook template**: Previously missing from `config-templates/`, now shipped inside the package. Closes the gap where README + CHANGELOG advertised a 7-hook stack but `setup-hooks.py` only installed 6.
- **Package data**: `memorymaster/config_templates/hooks/*.py` and `memorymaster/config_templates/*.md` are now included in the wheel via `[tool.setuptools.package-data]`. `setup_hooks.py` locates templates via `importlib.resources.files("memorymaster")` so it works from both wheel and editable installs.

### Fixed

- **Delete phantom `dict[str` file** from repo root (0-byte file tracked since commit `1d1c33c` via a shell parsing accident).
- **Relax `quick` SLO thresholds** in `benchmarks/slo_targets.json` to survive GitHub Actions runner variance. Observed up to 10x p95 swings between consecutive runs on the same commit (query_p95: 0.053s vs 0.512s, throughput: 19.5 vs 9.9 ops/s). The old thresholds were calibrated against a single lucky run and made CI flaky. New ceilings provide ~20% headroom over the worst observed value; a `_comment` field in the JSON documents the rationale.
- **Align docs with CI install set**: `INSTALLATION.md` troubleshooting previously told users to install `.[dev,mcp,security,embeddings,qdrant]` while CI runs `.[dev,mcp,security]`. The minimal set is the canonical reproduction environment (optional extras skip automatically via `pytest.importorskip`). Docs now match CI.

### Changed

- **Templates moved from `config-templates/` to `memorymaster/config_templates/`**: Required for wheel distribution. `scripts/setup-hooks.py` becomes a shim. README and INSTALLATION.md now document both the `memorymaster-setup` flow (recommended for pip-installed users) and the clone workflow.
- **README + INSTALLATION.md**: Document the 7-hook stack, the `memorymaster-setup` entry point, and the fact that CI uses `.[dev,mcp,security]`.

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
