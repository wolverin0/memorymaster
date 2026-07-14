# STRUCTURE.md — Directory Structure (R4.1 boundary refresh)

The authoritative core is split across `memorymaster/core`, `stores`, `recall`,
and `govern`. Optional integrations live in the built-in companion namespaces
`memorymaster/bridges` and the `memorymaster/knowledge/wiki_*` / `vault_*`
modules. `memorymaster/surfaces` is the composition root. Core-to-companion
imports are forbidden and pinned by `tests/test_extension_boundaries.py`.

```
G:/_OneDrive/OneDrive/Desktop/Py Apps/memorymaster/
├── pyproject.toml / pytest.ini      # Build config, entry points, test config
├── memorymaster.db (+ -wal/-shm)    # Live SQLite DB (FTS5 + WAL, ~3GB, in production use)
├── CHANGELOG.md / docs/             # Release history, extended docs
├── memorymaster/                    # Core package (~110 modules)
│   ├── service.py                   # MemoryService — ingest, query_rows, run_cycle, dedup, pin
│   ├── storage.py + _storage_*.py   # SQLiteStore split into mixins: schema, read, write_claims,
│   │                                #   lifecycle, sources, shared (files kept under 800 LOC)
│   ├── postgres_store.py / store_factory.py  # Postgres parity store + backend selection
│   ├── schema.sql / schema_postgres.sql / schema.py / models.py  # DDL + dataclasses + CLAIM_STATUSES
│   ├── migrations/                  # Versioned migrations (0001-0007) + checksum-verified runner.py
│   ├── jobs/                        # Cycle jobs: extractor, candidate dedupe (pre-steward),
│   │                                #   deterministic, validator, decay, compactor, dedup,
│   │                                #   staleness, calibration, compact_summaries, daydream_ingest
│   ├── mcp_server.py                # FastMCP stdio server (~30 tools) + auto-citation
│   ├── mcp_path_policy.py / mcp_usage.py  # Path allowlist for db overrides; per-tool usage stats
│   ├── cli.py / cli_handlers_*.py / cli_helpers.py  # CLI dispatch (basic + curation handler sets)
│   ├── llm_steward.py               # LLM steward: candidate extraction/curation, key rotation, scope filter
│   ├── steward_classifier.py / steward_features.py / steward.py  # Calibrated promotion classifier
│   ├── llm_provider.py / llm_budget.py / llm_rerank.py  # Multi-provider client, cycle caps, reranker
│   ├── context_hook.py              # Recall hook: 6-stream fusion + RRF auto-gate + telemetry
│   ├── retrieval.py / recall_fusion.py / recall_tokenizer.py  # Linear ranker, RRF, FTS5 tokenizer
│   ├── query_classifier.py / query_expansion.py / query_cache.py / context_optimizer.py
│   ├── embeddings.py / qdrant_backend.py / qdrant_recall_fallback.py  # Vector layer
│   ├── graph_store.py / entity_graph.py / entity_extractor.py / entity_registry.py  # Entity+graph streams
│   ├── verbatim_store.py / verbatim_recall.py / verbatim_cleanup.py  # Raw-conversation archive
│   ├── rules.py / rule_miner.py / rule_export.py  # Rule-shaped claims mined from corrections
│   ├── wiki_engine.py / wiki_validate.py / wiki_freshness.py / wiki_similarity.py / wiki_suggest.py
│   ├── vault_linter.py / vault_bases.py / vault_exporter.py / vault_curator.py / vault_synthesis.py
│   ├── vault_log.py / vault_query_capture.py / daily_notes.py  # Vault ops log, query capture, summaries
│   ├── conflict_resolver.py / auto_resolver.py / contradiction_probe.py / claim_verifier.py
│   ├── lifecycle.py / claim_edges.py / closets.py / feedback.py / rl_trainer.py  # Lifecycle + quality signals
│   ├── access_control.py / security.py / dashboard_auth.py / key_rotator.py  # RBAC, sensitivity, auth
│   ├── dashboard.py / observability.py / metrics_exporter.py / hook_log.py  # Web UI + telemetry
│   ├── db_merge.py / delta_sync.py / dream_bridge.py / snapshot.py  # Sync + git-backed DB versioning
│   ├── atlas_contract.py / atlas_claim_extractor.py / action_extractor.py / action_exporters.py
│   ├── media_processing.py / media_providers.py / connectors/whatsapp.py  # Atlas Inbox evidence
│   ├── transcript_miner.py / turn_schema.py / session_tracker.py  # Transcript -> claims pipeline
│   ├── operator.py / operator_queue.py / scheduler.py / webhook.py / retry.py
│   ├── scope_utils.py / config.py / policy.py / federated_graphify.py / qmd_bridge.py
│   ├── setup_hooks.py / config_templates/  # Installer + hook/MD templates for Claude/Codex
│   └── skill_evolver.py / auto_extractor.py / candidate_dedupe.py / review.py
├── tests/                           # Pytest suite (~228 test files)
├── scripts/                         # ~56 utilities: importers (*_to_turns), Qdrant indexers,
│                                    #   recall/steward evals, backfills, sync, recovery
├── benchmark/ / benchmarks/         # LongMemEval QA set + recall benchmark harnesses
├── config-templates/                # Hook templates consumed by scripts/setup-hooks.py
├── obsidian-vault/
│   ├── wiki/                        # Active wiki articles by project scope (compiled truth + timeline)
│   ├── raw/                         # Staging for Obsidian Clipper / manual ingestion
│   └── bases/                       # Generated Obsidian Bases (.base) — regenerated on wiki-absorb
├── docker-compose*.yml / Dockerfile / helm/  # Container + k8s deployment
├── .github/workflows/ci.yml         # CI
├── .planning/codebase/              # These planning docs
├── graphify-out/ / .gitnexus/       # Code-intelligence outputs (GRAPH_REPORT, GitNexus index)
└── artifacts/ / data/ / debug/ / handoffs/  # Run artifacts, working data, session handoffs
```

Notes
- The package root also holds transient working files (out.txt, err.txt, *.db.corrupt-*, recovery
  scripts) from live operations; they are not part of the architecture.
- Storage and CLI were deliberately split into `_storage_*` / `cli_handlers_*` modules to keep
  every file under 800 LOC.
- Migration 0005 does not exist; runner.py tolerates gaps and verifies checksums (drift detection).
- The retired generic plugin registry is not a supported extension surface.
  Optional integrations use the typed provider protocols and companion package
  boundaries documented in `docs/architecture.md`.
