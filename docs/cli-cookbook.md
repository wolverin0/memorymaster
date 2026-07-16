# MemoryMaster CLI Cookbook

Every example below uses the current `memorymaster/cli.py` argparse surface and keeps the default `memorymaster.db` database path explicit.

For a fresh local database, run `init-db` first. Examples that reference claim IDs, snapshot IDs, source item IDs, or input files assume those records or files already exist in the session.

Requested aliases checked against the parser: `restore` is currently `rollback`, `redact-claim-payload` is currently `redact-claim`, and `list-steward-proposals` is currently `steward-proposals`. `query-meta-decisions`, `find-related`, and standalone `decay` are not CLI subcommands in the current parser.

Coverage was checked by comparing every `###` heading with `build_parser()` subparser choices and parsing each example command with argparse.

## Query & Recall

### query
**Purpose**: Search claims by text.
**Example**:
```
python -m memorymaster --db memorymaster.db query "sqlite wal mode" --limit 10 --scope-allowlist project:memorymaster
```

### context
**Purpose**: Pack relevant claims into a token-budgeted context block.
**Example**:
```
python -m memorymaster --db memorymaster.db context "MemoryMaster CLI architecture" --budget 2000 --format text --limit 25
```

### recall
**Purpose**: Query memory for pre-turn context injection.
**Example**:
```
python -m memorymaster --db memorymaster.db recall "current project constraints" --budget 1500 --format text
```

### federated-query
**Purpose**: Query claims across all scopes.
**Example**:
```
python -m memorymaster --db memorymaster.db federated-query "sqlite wal" --limit 10
```

### ready
**Purpose**: Show stale, conflicted, and low-confidence claims needing attention.
**Example**:
```
python -m memorymaster --db memorymaster.db ready --limit 5 --confidence-threshold 0.6
```

### history
**Purpose**: Show the audit timeline for a single claim.
**Example**:
```
python -m memorymaster --db memorymaster.db history 1 --limit 20
```

### links
**Purpose**: Show all typed links for a claim.
**Example**:
```
python -m memorymaster --db memorymaster.db links 1 --type relates_to
```

### qdrant-search
**Purpose**: Reserved for governed semantic retrieval. R1.3 temporarily disables this command; it exits with code 2 before constructing a Qdrant backend. Use `query` for authoritative lexical/hybrid recall.

### qdrant-sync
**Purpose**: Maintain the Qdrant index while retrieval is quarantined.
**Example**:
```
python -m memorymaster --db memorymaster.db qdrant-sync
```

### qdrant-reconcile
**Purpose**: Compare the authoritative store with the Qdrant maintenance index and repair drift/orphans. This does not enable claim or verbatim payload retrieval.
**Example**:
```
python -m memorymaster --db memorymaster.db qdrant-reconcile
```

## Ingest & Lifecycle

### init-db
**Purpose**: Create the database schema.
**Example**:
```
python -m memorymaster --db memorymaster.db init-db
```

### ingest
**Purpose**: Ingest a raw claim with citations.
**Example**:
```
python -m memorymaster --db memorymaster.db ingest --text "WAL mode is mandatory for SQLite stores." --source "docs/cli-cookbook.md|MemoryMaster CLI Cookbook|WAL mode is mandatory" --claim-type constraint --subject sqlite --predicate requires --object wal --scope project:memorymaster
```

### observe
**Purpose**: Extract and ingest observations from text.
**Example**:
```
python -m memorymaster --db memorymaster.db observe --text "Use wiki-absorb after claim ingestion." --source session --scope project:memorymaster --force
```

### extract-claims
**Purpose**: Extract structured claims from unstructured text.
**Example**:
```
python -m memorymaster --db memorymaster.db extract-claims --input "MemoryMaster uses SQLite WAL mode." --source notes --scope project:memorymaster
```

### mine-transcript
**Purpose**: Parse Claude Code transcripts into claims.
**Example**:
```
python -m memorymaster --db memorymaster.db mine-transcript --input transcripts/session.jsonl --scope project:memorymaster --max 25
```

### import-whatsapp
**Purpose**: Import WhatsApp messages from a wacli JSON or JSONL export.
**Example**:
```
python -m memorymaster --db memorymaster.db import-whatsapp --input exports/whatsapp.jsonl --display-name WhatsApp --chat-id project-chat
```

### run-cycle
**Purpose**: Run extractor, validator, decay, and optional compaction.
**Example**:
```
python -m memorymaster --db memorymaster.db run-cycle --with-compact --min-citations 1 --policy-mode cadence
```

### run-steward
**Purpose**: Run claim stewardship probes and proposal generation.
**Example**:
```
python -m memorymaster --db memorymaster.db run-steward --mode manual --max-claims 100 --artifact-json artifacts/steward/steward_report.json
```

### compact
**Purpose**: Archive stale, superseded, or conflicted claims and trim old events.
**Example**:
```
python -m memorymaster --db memorymaster.db compact --retain-days 30 --event-retain-days 60
```

### compact-summaries
**Purpose**: Summarize archived claim clusters into higher-level summary claims.
**Example**:
```
python -m memorymaster --db memorymaster.db compact-summaries --provider ollama --model llama3.1 --dry-run --limit 100
```

### dedup
**Purpose**: Detect and merge duplicate claims by embedding similarity.
**Example**:
```
python -m memorymaster --db memorymaster.db dedup --threshold 0.92 --min-text-overlap 0.3 --dry-run
```

### recompute-tiers
**Purpose**: Recompute core, working, and peripheral memory tiers for all claims.
**Example**:
```
python -m memorymaster --db memorymaster.db recompute-tiers
```

### quality-scores
**Purpose**: Recompute quality scores for all claims.
**Example**:
```
python -m memorymaster --db memorymaster.db quality-scores
```

### train-model
**Purpose**: Train the quality prediction model from feedback data.
**Example**:
```
python -m memorymaster --db memorymaster.db train-model
```

## Wiki

### wiki-absorb
**Purpose**: Absorb claims into wiki articles.
**Example**:
```
python -m memorymaster --db memorymaster.db wiki-absorb --output obsidian-vault --scope project:memorymaster
```

### lint-vault
**Purpose**: Detect wiki contradictions, orphans, gaps, and stale claims.
**Example**:
```
python -m memorymaster --db memorymaster.db lint-vault --scope project:memorymaster --no-llm --max-stale-days 30
```

### wiki-cleanup
**Purpose**: Audit and rewrite weak wiki articles.
**Example**:
```
python -m memorymaster --db memorymaster.db wiki-cleanup --output obsidian-vault --scope project:memorymaster
```

### wiki-breakdown
**Purpose**: Find and create missing wiki articles.
**Example**:
```
python -m memorymaster --db memorymaster.db wiki-breakdown --output obsidian-vault --scope project:memorymaster
```

### wiki-freshness
**Purpose**: Report per-article wiki freshness.
**Example**:
```
python -m memorymaster --db memorymaster.db wiki-freshness --vault obsidian-vault/wiki --below 0.7
```

### wiki-backfill-bindings
**Purpose**: Backfill `claims.wiki_article` from existing wiki frontmatter.
**Example**:
```
python -m memorymaster --db memorymaster.db wiki-backfill-bindings --output obsidian-vault
```

### wiki-suggest-links
**Purpose**: Suggest wiki article links from paragraph entities.
**Example**:
```
python -m memorymaster --db memorymaster.db wiki-suggest-links --text "SQLite WAL prevents concurrent write corruption." --wiki-root obsidian-vault/wiki --limit 5 --hops 2
```

### bases-generate
**Purpose**: Regenerate Obsidian Bases files for the wiki.
**Example**:
```
python -m memorymaster --db memorymaster.db bases-generate --output obsidian-vault
```

### export-vault
**Purpose**: Export claims as Obsidian-compatible Markdown files.
**Example**:
```
python -m memorymaster --db memorymaster.db export-vault --output obsidian-vault/raw --scope project:memorymaster --confirmed-only
```

### curate-vault
**Purpose**: Generate an LLM-curated Obsidian vault.
**Example**:
```
python -m memorymaster --db memorymaster.db curate-vault --output obsidian-vault/curated --scope project:memorymaster --dry-run
```

## Operations

### stealth-status
**Purpose**: Show whether stealth mode is active and which database is in use.
**Example**:
```
python -m memorymaster --db memorymaster.db stealth-status
```

### snapshot
**Purpose**: Create a versioned snapshot of the claim database.
**Example**:
```
python -m memorymaster --db memorymaster.db snapshot --message "before steward run"
```

### snapshots
**Purpose**: List database snapshots.
**Example**:
```
python -m memorymaster --db memorymaster.db snapshots
```

### rollback
**Purpose**: Restore the database from a snapshot.
**Example**:
```
python -m memorymaster --db memorymaster.db rollback abc123 --yes
```

### diff
**Purpose**: Show claims added, removed, or changed since a snapshot.
**Example**:
```
python -m memorymaster --db memorymaster.db diff abc123
```

### redact-claim
**Purpose**: Redact or erase claim payload fields with audit history.
**Example**:
```
python -m memorymaster --db memorymaster.db redact-claim 1 --mode redact --claims-only --reason "support ticket" --actor cli
```

### recompute-confidence-priors
**Purpose**: Write recommended initial-confidence priors from validator events.
**Example**:
```
python -m memorymaster --db memorymaster.db recompute-confidence-priors --window-days 90 --output docs/calibration-priors.json
```

### install-hook
**Purpose**: Install a git post-commit hook that snapshots the database.
**Example**:
```
python -m memorymaster --db memorymaster.db install-hook
```

### install-gitnexus-hook
**Purpose**: Install a GitNexus post-commit hook that re-analyzes the project.
**Example**:
```
python -m memorymaster --db memorymaster.db install-gitnexus-hook
```

### qdrant-sync
**Purpose**: Bulk-sync active claims to the Qdrant maintenance index. Indexed payloads are not a retrieval authority during R1.3.
**Example**:
```
python -m memorymaster --db memorymaster.db qdrant-sync --qdrant-url http://localhost:6333 --ollama-url http://localhost:11434
```

### merge-db
**Purpose**: Merge claims from another MemoryMaster database.
**Example**:
```
python -m memorymaster --db memorymaster.db merge-db --source backups/memorymaster-remote.db
```

### mcp-usage-report
**Purpose**: Export MCP tool usage for a time window.
**Example**:
```
python -m memorymaster --db memorymaster.db mcp-usage-report --since 14d --format csv
```

Use this report to decide whether the optional Everything integration earns its
maintenance cost. `local_search:exact`, `local_search:fuzzy`, and
`resolve_project` rows record calls, result status, and latency without storing
the query or returned path. Compare useful-hit rate and latency over a real
two-week window; disable the integration if it is rarely called or mostly empty.

### local-search / resolve-project
**Purpose**: Search the local Everything index without writing claims, or resolve
a fuzzy project alias. Both commands are read-only by default.
**Examples**:
```
python -m memorymaster --db memorymaster.db local-search AGENTS.md --kind file --exact
python -m memorymaster --db memorymaster.db resolve-project memorymaster
python -m memorymaster --db memorymaster.db resolve-project memorymaster --remember
```

`--exact` requests a whole-name match. `--remember` is explicit and persists only
non-sensitive matches meeting the calibrated `0.85` confidence threshold.

## Diagnostics

### list-claims
**Purpose**: List claims with optional status filtering.
**Example**:
```
python -m memorymaster --db memorymaster.db list-claims --status confirmed --limit 20
```

### list-events
**Purpose**: List claim lifecycle events.
**Example**:
```
python -m memorymaster --db memorymaster.db list-events --event-type ingest --limit 20
```

### steward-proposals
**Purpose**: List steward proposal events for human override.
**Example**:
```
python -m memorymaster --db memorymaster.db steward-proposals --limit 50 --include-resolved
```

### review-queue
**Purpose**: Build a conflict and stale-claim review queue.
**Example**:
```
python -m memorymaster --db memorymaster.db review-queue --limit 25 --exclude-conflicted
```

### atlas-version
**Purpose**: Print the Atlas API and CLI contract version.
**Example**:
```
python -m memorymaster --db memorymaster.db atlas-version
```

### action-proposals
**Purpose**: List Atlas action proposals.
**Example**:
```
python -m memorymaster --db memorymaster.db action-proposals --status candidate --destination super-productivity --limit 25
```

### list-media-retries
**Purpose**: List media retry queue rows.
**Example**:
```
python -m memorymaster --db memorymaster.db list-media-retries --status pending --limit 20
```

### feedback-stats
**Purpose**: Show feedback tracking and quality score statistics.
**Example**:
```
python -m memorymaster --db memorymaster.db feedback-stats
```

### entity-stats
**Purpose**: Show entity graph statistics.
**Example**:
```
python -m memorymaster --db memorymaster.db entity-stats
```

### sessions
**Purpose**: List active and recent agent sessions.
**Example**:
```
python -m memorymaster --db memorymaster.db sessions
```

### ghost-notes
**Purpose**: Find frequently queried topics with few claims.
**Example**:
```
python -m memorymaster --db memorymaster.db ghost-notes
```

## Atlas & Media Intake

### propose-actions
**Purpose**: Create reviewable action proposals from source evidence.
**Example**:
```
python -m memorymaster --db memorymaster.db propose-actions --destination super-productivity --limit 100
```

### extract-atlas-claims
**Purpose**: Extract candidate claims from Atlas evidence.
**Example**:
```
python -m memorymaster --db memorymaster.db extract-atlas-claims --scope project:memorymaster --limit 100
```

### resolve-action-proposal
**Purpose**: Update an Atlas action proposal status.
**Example**:
```
python -m memorymaster --db memorymaster.db resolve-action-proposal --proposal-id 1 --status approved --external-ref task-123
```

### edit-action-proposal
**Purpose**: Edit user-facing fields on an Atlas action proposal.
**Example**:
```
python -m memorymaster --db memorymaster.db edit-action-proposal --proposal-id 1 --title "Review stale claims" --description "Triage low-confidence memories" --confidence 0.8
```

### label-source-item
**Purpose**: Set a sensitivity label on an Atlas source item.
**Example**:
```
python -m memorymaster --db memorymaster.db label-source-item --source-item-id 1 --sensitivity low
```

### label-evidence-item
**Purpose**: Set a sensitivity label on an Atlas evidence item.
**Example**:
```
python -m memorymaster --db memorymaster.db label-evidence-item --evidence-item-id 1 --sensitivity low
```

### enqueue-media-retry
**Purpose**: Enqueue a media retry row for LifeAgent.
**Example**:
```
python -m memorymaster --db memorymaster.db enqueue-media-retry --source-item-id 1 --media-key msg-123 --chat-id project-chat --media-type audio
```

### process-media-retry-queue
**Purpose**: Claim pending media retry rows for processing.
**Example**:
```
python -m memorymaster --db memorymaster.db process-media-retry-queue --limit 10
```

### record-media-retry-outcome
**Purpose**: Record the fetch result for a media retry row.
**Example**:
```
python -m memorymaster --db memorymaster.db record-media-retry-outcome --retry-id 1 --status failed --last-http-status 404 --last-error "missing media"
```

### transcribe-source-item
**Purpose**: Transcribe a source item through the selected provider.
**Example**:
```
python -m memorymaster --db memorymaster.db transcribe-source-item --source-item-id 1 --provider openai
```

### ocr-source-item
**Purpose**: Run OCR on a source item through the selected provider.
**Example**:
```
python -m memorymaster --db memorymaster.db ocr-source-item --source-item-id 1 --provider tesseract
```

Mock providers require both `MEMORYMASTER_MEDIA_MODE=test` (or `development`) and `MEMORYMASTER_ALLOW_SYNTHETIC_MEDIA=1`. They never feed governed claims, actions, citations, or exports.

### export-actions
**Purpose**: Export approved Atlas action proposals.
**Example**:
```
python -m memorymaster --db memorymaster.db export-actions --output exports/actions.json --destination super-productivity --limit 50 --dry-run
```

## Claim Curation

### pin
**Purpose**: Pin or unpin a claim.
**Example**:
```
python -m memorymaster --db memorymaster.db pin 1
```

### link
**Purpose**: Create a typed link between two claims.
**Example**:
```
python -m memorymaster --db memorymaster.db link 1 2 --type relates_to
```

### unlink
**Purpose**: Remove links between two claims.
**Example**:
```
python -m memorymaster --db memorymaster.db unlink 1 2 --type relates_to
```

### resolve-conflicts
**Purpose**: Detect and optionally auto-resolve conflicting claims.
**Example**:
```
python -m memorymaster --db memorymaster.db resolve-conflicts --dry-run --limit 100
```

### check-staleness
**Purpose**: Detect claims whose cited source files changed.
**Example**:
```
python -m memorymaster --db memorymaster.db check-staleness --mode mtime --dry-run --limit 100
```

### resolve-proposal
**Purpose**: Approve or reject a steward proposal.
**Example**:
```
python -m memorymaster --db memorymaster.db resolve-proposal --action approve --proposal-event-id 1 --no-apply
```

### auto-resolve
**Purpose**: Use an LLM to resolve conflicted claims.
**Example**:
```
python -m memorymaster --db memorymaster.db auto-resolve --limit 10
```

### verify-claims
**Purpose**: Cross-check claims against the current codebase.
**Example**:
```
python -m memorymaster --db memorymaster.db verify-claims --scope project:memorymaster --limit 100
```

### extract-entities
**Purpose**: Run LLM entity extraction on claims.
**Example**:
```
python -m memorymaster --db memorymaster.db extract-entities --status confirmed --limit 50
```

### entity-list
**Purpose**: List canonical entities with alias and claim counts.
**Example**:
```
python -m memorymaster --db memorymaster.db entity-list --scope project:memorymaster --type project --limit 20
```

### entity-merge
**Purpose**: Merge one entity into another.
**Example**:
```
python -m memorymaster --db memorymaster.db entity-merge 1 2
```

### entity-aliases
**Purpose**: List or add aliases for an entity.
**Example**:
```
python -m memorymaster --db memorymaster.db entity-aliases 1 --add MemoryMaster
```

### entity-backfill
**Purpose**: Backfill `entity_id` on claims with subjects but no entity.
**Example**:
```
python -m memorymaster --db memorymaster.db entity-backfill
```

### entity-graph-export
**Purpose**: Export entity graph relationships as DOT or GraphML.
**Example**:
```
python -m memorymaster --db memorymaster.db entity-graph-export --format dot --output artifacts/entity-graph.dot --scope project:memorymaster
```

## Runtime Services

### run-daemon
**Purpose**: Run the scheduler loop for periodic memory maintenance.
**Example**:
```
python -m memorymaster --db memorymaster.db run-daemon --interval-seconds 300 --max-cycles 1 --compact-every 6
```

### run-dashboard
**Purpose**: Run the read-only HTTP dashboard and API.
**Example**:
```
python -m memorymaster --db memorymaster.db run-dashboard --host 127.0.0.1 --port 8765
```

### run-operator
**Purpose**: Run the pre/post-turn memory maintenance loop from a JSONL inbox.
**Example**:
```
python -m memorymaster --db memorymaster.db run-operator --inbox-jsonl queue/inbox.jsonl --max-events 25 --max-idle-seconds 5 --no-state
```

## Reports & Exports

### export-metrics
**Purpose**: Export D3 structured metrics from JSONL events.
**Example**:
```
python -m memorymaster --db memorymaster.db export-metrics --events-jsonl artifacts/events.jsonl --out-prom artifacts/metrics.prom --out-json artifacts/metrics.json
```

### daily-note
**Purpose**: Generate a daily note summarizing activity.
**Example**:
```
python -m memorymaster --db memorymaster.db daily-note --date 2026-05-12 --output obsidian-vault/daily
```

## Dream & Agent Memory

### dream-seed
**Purpose**: Export MemoryMaster claims into Claude Code Auto Dream files.
**Example**:
```
python -m memorymaster --db memorymaster.db dream-seed --project . --min-tier 2 --min-quality 0.5 --max 25 --dry-run
```

### dream-ingest
**Purpose**: Import Auto Dream memories back into MemoryMaster.
**Example**:
```
python -m memorymaster --db memorymaster.db dream-ingest --project .
```

### dream-sync
**Purpose**: Bidirectionally sync MemoryMaster and Auto Dream memories.
**Example**:
```
python -m memorymaster --db memorymaster.db dream-sync --project . --min-tier 2 --min-quality 0.5 --max 25
```

### dream-clean
**Purpose**: Remove MemoryMaster-prefixed files from the Claude Code memory directory.
**Example**:
```
python -m memorymaster --db memorymaster.db dream-clean --project . --dry-run
```
