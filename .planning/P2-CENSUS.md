# P2 Census Scorecard — MemoryMaster module-by-module

Date: 2026-06-10 | Branch: omni/p2-restructure (analysis on omni/p1-reliability worktree) | Modules: 145 (.py under `memorymaster/`, excl. `__pycache__`/graphify-out)

Inputs: 4 analyst reports (import-graph AST/BFS+Tarjan, git activity census, external-surface sweep, clustering=null) cross-checked against a fresh AST fan-in pass + single-pass `git log --name-only` last-touch + filesystem LOC (this document's own throwaway script; numbers below are the verified pass).

**Fan-in** = internal importers inside the package (top-level + lazy fn-level imports, AST-resolved). External surface (CLI/MCP/hook/script/entrypoint) is listed separately — fan-in 0 does NOT mean dead.

**Verdict rules (operator-mandated, conservative):** kill ONLY if zero import reachability AND zero external surface AND dormant. `dormant_6mo` from the activity census is EMPTY — the oldest module (2026-03-07) is 3 months old — therefore **no module qualifies for kill**. All six zero-reach/zero-surface orphans become MERGE candidates instead.

## Verdict summary

| Verdict | Count |
|---|---|
| keep | 139 |
| merge | 6 |
| kill | 0 |

## Module table

Legend: surface column — EP=console-script entrypoint, CLI=subcommand backing, MCP=tool backing, HOOK=imported by installed ~/.claude hook or schtask, SCRIPT=referenced by scripts/*, none=internal-only.

### root (stays in place)

| Module | Subpkg | Fan-in | Last | LOC | Surface | Verdict — evidence |
|---|---|---|---|---|---|---|
| `__init__.py` | root | 17 | 2026-06-09 | 5 | import root | keep — package root, re-exports |
| `__main__.py` | root | 0 | 2026-03-07 | 3 | `python -m memorymaster` | keep — must stay at root for `-m` |
| `config_templates/hooks/memorymaster-auto-ingest.py` | root | 0 | 2026-06-10 | 318 | HOOK template (Stop, settings.json:88) | keep — production template; drift-checked by scripts/check_hook_template_drift.py |
| `config_templates/hooks/memorymaster-classify.py` | root | 0 | 2026-04-23 | 304 | HOOK template (UserPromptSubmit) | keep — stdlib-only, installed |
| `config_templates/hooks/memorymaster-dream-sync.py` | root | 0 | 2026-06-10 | 40 | installed at ~/.claude/hooks but registered NOWHERE | keep — flag to operator: orphaned registration (setup_hooks.py:147 comment only) |
| `config_templates/hooks/memorymaster-precompact.py` | root | 0 | 2026-05-04 | 105 | HOOK template (PreCompact) | keep |
| `config_templates/hooks/memorymaster-recall.py` | root | 0 | 2026-06-10 | 40 | HOOK template (UserPromptSubmit, production recall) | keep |
| `config_templates/hooks/memorymaster-session-start.py` | root | 0 | 2026-04-23 | 272 | HOOK template (SessionStart) | keep — raw sqlite3, schema-coupled |
| `config_templates/hooks/memorymaster-steward-cycle.py` | root | 0 | 2026-06-10 | 75 | schtask MemoryMasterSteward every 6h, RUNNING | keep — production scheduled task |
| `config_templates/hooks/memorymaster-validate-wiki.py` | root | 0 | 2026-05-07 | 158 | HOOK template (PostToolUse) | keep — does NOT import wiki_validate.py (reimplements inline) |

### core/ (16)

| Module | Subpkg | Fan-in | Last | LOC | Surface | Verdict — evidence |
|---|---|---|---|---|---|---|
| models.py | core | 38 | 2026-05-07 | 442 | HOOK auto-ingest, 5 scripts | keep — 2nd highest fan-in; land in core first |
| service.py | core | 12 | 2026-06-10 | 1905 | MCP all tools, CLI all cmds, 2 hooks, 14 scripts, VM cron | keep — OVER 800, split plan below |
| lifecycle.py | core | 13 | 2026-05-11 | 72 | SCRIPT archive_watchkeeper_heartbeats | keep — SCC member; cut #1 = move absorb trigger out |
| config.py | core | 7 | 2026-06-01 | 451 | none | keep |
| policy.py | core | 2 | 2026-04-23 | 133 | CLI (via cli.py top-level) | keep |
| security.py | core | 14 | 2026-06-01 | 558 | HOOK auto-ingest, SCRIPT backfill | keep — sensitivity filter, never move without shim |
| scope_utils.py | core | 2 | 2026-04-27 | 109 | none | keep |
| observability.py | core | 5 | 2026-05-12 | 183 | MCP (top-level import) | keep |
| retry.py | core | 2 | 2026-03-08 | 88 | none | keep — small util |
| spool.py | core | 8 | 2026-06-10 | 201 | HOOK auto-ingest | keep — durable ingest buffer |
| llm_provider.py | core | 10 | 2026-06-01 | 696 | HOOK auto-ingest, SCRIPT label_prompts | keep — cycle with llm_steward (lazy both ways); fix by moving KeyRotator |
| key_rotator.py | core | 1 | 2026-04-22 | 176 | none (llm_provider lazy) | keep — absorb llm_steward's KeyRotator/DEFAULT_COOLDOWN here to break cycle |
| access_control.py | core | 1 | 2026-03-21 | 126 | none (service.py:834 lazy) | keep — reached from service |
| webhook.py | core | 2 | 2026-06-01 | 172 | none (service:514, jobs/integrity:183 lazy) | keep |
| hook_log.py | core | 3 | 2026-04-23 | 65 | HOOK recall + dream-sync | keep — hook-surface, shim mandatory |
| plugins.py | core | 0 | 2026-03-21 | 123 | tests/test_plugins.py only | **merge -> core (or drop after operator sign-off)** — zero callers of register_plugin/get_plugins in package; docstring entry-point group consumed by nothing live; NOT dormant (created 2026-03-21 < 6mo) so kill blocked by rule |

### stores/ (19)

| Module | Subpkg | Fan-in | Last | LOC | Surface | Verdict — evidence |
|---|---|---|---|---|---|---|
| storage.py | stores | 3 | 2026-06-10 | 181 | SCRIPT archive_watchkeeper | keep — facade over _storage_* mixins |
| _storage_shared.py | stores | 40 | 2026-06-10 | 140 | HOOK steward-cycle (open_conn) | keep — HIGHEST fan-in in package (28% of modules); move first within stores, shim mandatory |
| _storage_lifecycle.py | stores | 1 | 2026-06-10 | 643 | none | keep |
| _storage_read.py | stores | 1 | 2026-06-01 | 701 | none | keep |
| _storage_schema.py | stores | 1 | 2026-06-10 | 867 | none | keep — OVER 800, split plan below |
| _storage_sources.py | stores | 2 | 2026-06-01 | 1006 | none | keep — OVER 800, split plan below |
| _storage_write_claims.py | stores | 1 | 2026-05-12 | 374 | none | keep |
| postgres_store.py | stores | 1 | 2026-06-01 | 2613 | CLI --db DSN routing, smoke_postgres.ps1 | keep — LARGEST file, split plan below |
| store_factory.py | stores | 4 | 2026-06-10 | 27 | CLI/steward DSN routing | keep — SCC member; cut #2 = invert llm_steward->store_factory |
| schema.py | stores | 2 | 2026-03-07 | 11 | none | keep — 11-LOC stub; fold into _storage_schema during move (intra-stores tidy, not counted as merge verdict) |
| snapshot.py | stores | 2 | 2026-06-10 | 487 | CLI snapshot/rollback/diff/install-hook | keep |
| migrations/__init__.py | stores | 3 | 2026-06-10 | 48 | CLI migrate | keep |
| migrations/runner.py | stores | 1 | 2026-06-10 | 268 | CLI migrate (importlib loads 000*) | keep — dynamic loader; static fan-in misleads |
| migrations/0001_initial.py | stores | 0* | 2026-05-17 | 25 | CLI migrate (importlib) | keep — *dynamically loaded, runner.py:90 |
| migrations/0002_miner_state.py | stores | 0* | 2026-05-21 | 38 | CLI migrate (importlib) | keep |
| migrations/0003_contradiction_verdicts.py | stores | 0* | 2026-05-24 | 42 | CLI migrate (importlib) | keep |
| migrations/0004_query_cache.py | stores | 0* | 2026-05-24 | 114 | CLI migrate (importlib) | keep |
| migrations/0006_verbatim_session_content_index.py | stores | 0* | 2026-06-01 | 59 | CLI migrate (importlib) | keep — NOTE: numbering gap, 0005 does not exist |
| migrations/0007_rule_stats.py | stores | 0* | 2026-06-09 | 54 | CLI migrate (importlib) | keep |

### recall/ (16)

| Module | Subpkg | Fan-in | Last | LOC | Surface | Verdict — evidence |
|---|---|---|---|---|---|---|
| context_hook.py | recall | 3 | 2026-06-10 | 2185 | HOOK recall (production), CLI recall/observe, MCP query_for_task, 4 eval scripts | keep — OVER 800, split plan below; shim mandatory |
| retrieval.py | recall | 2 | 2026-06-09 | 528 | CLI (top-level) | keep |
| recall_fusion.py | recall | 2 | 2026-04-23 | 79 | none | keep |
| recall_tokenizer.py | recall | 2 | 2026-06-01 | 329 | 5 eval scripts | keep |
| query_cache.py | recall | 1 | 2026-06-10 | 182 | none | keep |
| query_expansion.py | recall | 1 | 2026-04-23 | 184 | none | keep |
| query_classifier.py | recall | 3 | 2026-06-01 | 73 | MCP classify_query, CLI query | keep |
| context_optimizer.py | recall | 2 | 2026-05-11 | 395 | CLI (top-level) | keep |
| embeddings.py | recall | 6 | 2026-06-01 | 217 | none | keep |
| qdrant_backend.py | recall | 4 | 2026-06-10 | 379 | CLI qdrant-sync/search, MCP query_memory vector path | keep |
| qdrant_recall_fallback.py | recall | 1 | 2026-04-23 | 305 | none | keep — single-commit but imported (fan-in 1) |
| verbatim_store.py | recall | 3 | 2026-06-10 | 563 | HOOK auto-ingest, MCP search_verbatim | keep — shim mandatory |
| verbatim_recall.py | recall | 1 | 2026-04-23 | 296 | SCRIPT eval_recall_quality | keep |
| llm_rerank.py | recall | 1 | 2026-06-01 | 201 | none (service lazy, 2 sites) | keep |
| graph_store.py | recall | 1 | 2026-04-25 | 570 | SCRIPT backfill_graph_store | keep — context_hook.py:620 lazy |
| claim_edges.py | recall | 1 | 2026-06-10 | 285 | CLI link/unlink/links/query-paths | keep — context_hook.py:1554 lazy walk |

### govern/ (31)

| Module | Subpkg | Fan-in | Last | LOC | Surface | Verdict — evidence |
|---|---|---|---|---|---|---|
| steward.py | govern | 3 | 2026-06-01 | 1739 | CLI run-steward, MCP run_steward/proposals | keep — OVER 800, split plan below |
| steward_classifier.py | govern | 1 | 2026-04-29 | 152 | 3 training scripts | keep |
| steward_features.py | govern | 1 | 2026-04-29 | 344 | 3 training scripts | keep |
| llm_steward.py | govern | 3 | 2026-06-10 | 1074 | EP memorymaster-steward, CLI compact-summaries | keep — OVER 800; donate KeyRotator to core to break llm_provider cycle |
| llm_budget.py | govern | 7 | 2026-05-17 | 190 | none | keep — spend-cap, 7 importers |
| scheduler.py | govern | 1 | 2026-05-12 | 107 | CLI run-daemon | keep |
| review.py | govern | 2 | 2026-03-07 | 121 | CLI review-queue | keep |
| feedback.py | govern | 5 | 2026-06-10 | 246 | CLI feedback-stats, MCP quality_scores | keep |
| rl_trainer.py | govern | 1 | 2026-06-10 | 131 | CLI train-model | keep |
| auto_resolver.py | govern | 1 | 2026-06-01 | 253 | CLI auto-resolve | keep |
| conflict_resolver.py | govern | 1 | 2026-06-01 | 420 | CLI ready/resolve-conflicts | keep |
| candidate_dedupe.py | govern | 2 | 2026-06-01 | 301 | SCRIPT measure_dedupe_thresholds | keep |
| claim_verifier.py | govern | 1 | 2026-06-10 | 169 | CLI verify-claims | keep |
| contradiction_probe.py | govern | 2 | 2026-06-10 | 535 | CLI detect-contradictions | keep |
| verbatim_cleanup.py | govern | 1 | 2026-06-10 | 206 | CLI verbatim-cleanup | keep |
| jobs/__init__.py | govern | 3 | 2026-03-08 | 5 | submodule-via-package imports | keep |
| jobs/calibration.py | govern | 1 | 2026-05-11 | 120 | CLI confidence-priors | keep |
| jobs/compact_summaries.py | govern | 1 | 2026-05-12 | 442 | CLI compact-summaries | keep |
| jobs/compactor.py | govern | 3 | 2026-05-12 | 295 | CLI compact | keep |
| jobs/daydream_ingest.py | govern | 2 | 2026-05-17 | 375 | CLI ingest-daydream | keep |
| jobs/decay.py | govern | 3 | 2026-05-12 | 131 | CLI decay | keep |
| jobs/dedup.py | govern | 2 | 2026-05-12 | 398 | CLI dedup | keep |
| jobs/deterministic.py | govern | 3 | 2026-06-10 | 390 | run_cycle path | keep — only TOP-LEVEL edge into lifecycle inside SCC (jobs/deterministic.py:11) |
| jobs/entity_graph_export.py | govern | 1 | 2026-05-11 | 210 | CLI entity-graph-export | keep |
| jobs/extractor.py | govern | 2 | 2026-03-22 | 81 | run_cycle path | keep |
| jobs/fk_repair.py | govern | 1 | 2026-06-10 | 331 | CLI repair-fk (P1) | keep — single-commit but CLI-wired via cli_handlers_integrity |
| jobs/integrity.py | govern | 7 | 2026-06-10 | 346 | CLI integrity (P1) | keep — fan-in 7, P1 reliability core |
| jobs/qdrant_reconcile.py | govern | 3 | 2026-06-10 | 166 | CLI qdrant-reconcile (P1) | keep |
| jobs/spool_drain.py | govern | 3 | 2026-06-10 | 235 | CLI drain-spool (P1) | keep |
| jobs/staleness.py | govern | 1 | 2026-03-08 | 227 | CLI check-staleness | keep |
| jobs/validator.py | govern | 2 | 2026-06-10 | 252 | SCRIPT eval_steward_pareto | keep |

### knowledge/ (23)

| Module | Subpkg | Fan-in | Last | LOC | Surface | Verdict — evidence |
|---|---|---|---|---|---|---|
| wiki_engine.py | knowledge | 3 | 2026-06-10 | 964 | HOOK steward-cycle, CLI wiki-absorb/cleanup/breakdown | keep — OVER 800, split plan below; shim mandatory |
| wiki_freshness.py | knowledge | 2 | 2026-04-24 | 228 | CLI wiki-freshness | keep |
| wiki_similarity.py | knowledge | 1 | 2026-04-24 | 621 | steward + 3 training scripts | keep |
| wiki_suggest.py | knowledge | 1 | 2026-06-10 | 276 | CLI wiki-suggest-links | keep |
| wiki_validate.py | knowledge | 0 | 2026-06-01 | 299 | own `__main__` + 3 test files; validate-wiki hook does NOT import it | **merge -> knowledge/wiki tooling (or wire hook to it)** — zero package importers; borderline per surface analyst; has tests so not kill even ignoring dormancy rule |
| vault_bases.py | knowledge | 1 | 2026-04-10 | 234 | CLI bases-generate | keep |
| vault_curator.py | knowledge | 1 | 2026-06-10 | 295 | CLI curate-vault | keep |
| vault_exporter.py | knowledge | 1 | 2026-03-21 | 223 | CLI export-vault | keep |
| vault_linter.py | knowledge | 2 | 2026-06-10 | 451 | CLI lint-vault | keep — SCC member via lazy call_llm |
| vault_log.py | knowledge | 2 | 2026-04-04 | 108 | MCP ingest_claim side-effect (mcp_server:577) | keep |
| vault_synthesis.py | knowledge | 1 | 2026-04-04 | 152 | MCP ingest_claim side-effect (mcp_server:582) | keep |
| vault_query_capture.py | knowledge | 0 | 2026-06-01 | 108 | tests only; documented `query --save-to-vault` flag does not exist in cli.py | **merge -> knowledge/ vault tooling (or drop)** — zero importers, doc-only feature never wired; not dormant (< 6mo) so kill blocked |
| entity_extractor.py | knowledge | 3 | 2026-04-27 | 767 | SCRIPT backfill_entity_extraction | keep |
| entity_graph.py | knowledge | 5 | 2026-06-10 | 350 | CLI extract-entities, MCP extract_entities/entity_stats/find_related_claims | keep |
| entity_registry.py | knowledge | 5 | 2026-06-01 | 474 | CLI entity-list/merge/aliases/backfill | keep |
| rules.py | knowledge | 4 | 2026-05-20 | 104 | MCP ingest_rule | keep |
| rule_miner.py | knowledge | 3 | 2026-06-10 | 610 | HOOK auto-ingest, skill mm-mine-corrections, CLI mine-rules | keep — shim mandatory |
| rule_export.py | knowledge | 2 | 2026-06-10 | 161 | CLI export-rules, MCP rules_export | keep |
| transcript_miner.py | knowledge | 1 | 2026-06-10 | 156 | CLI mine-transcript | keep |
| auto_extractor.py | knowledge | 2 | 2026-03-23 | 139 | CLI extract-claims | keep |
| closets.py | knowledge | 1 | 2026-06-10 | 260 | CLI (via curation handlers) | keep |
| daily_notes.py | knowledge | 1 | 2026-06-10 | 191 | CLI daily-note/ghost-notes | keep |
| skill_evolver.py | knowledge | 0 | 2026-06-10 | 167 | NONE — zero imports, zero tests, zero scripts; documented `evolve-skills` subcommand does not exist in cli.py; repo grep hits only docs/architecture.md, .planning/codebase/STRUCTURE.md, CONTRIBUTING.md, debug/names.txt | **merge -> knowledge/ (operator decision: likely delete in P2 follow-up)** — strongest orphan (all 3 analysts agree zero reach + zero surface) but last-touched 2026-06-10 (3 commits), so NOT dormant; kill rule requires dormancy |

### surfaces/ (17)

| Module | Subpkg | Fan-in | Last | LOC | Surface | Verdict — evidence |
|---|---|---|---|---|---|---|
| cli.py | surfaces | 1 | 2026-06-10 | 670 | EP memorymaster, `__main__` | keep — 99 commits, hottest file |
| cli_handlers_basic.py | surfaces | 2 | 2026-06-09 | 1566 | CLI dispatch (~50 cmds) | keep — OVER 800, split plan below |
| cli_handlers_curation.py | surfaces | 1 | 2026-06-09 | 1062 | CLI dispatch (~35 cmds) | keep — OVER 800, split plan below |
| cli_handlers_integrity.py | surfaces | 1 | 2026-06-10 | 159 | CLI integrity/repair-fk/qdrant-reconcile/drain-spool | keep |
| cli_helpers.py | surfaces | 4 | 2026-05-07 | 151 | CLI shared helpers | keep |
| mcp_server.py | surfaces | 0 | 2026-06-09 | 1586 | EP memorymaster-mcp, 30 MCP tools, swap_race*.ps1 / FINISH_DB_SWAP.bat kill/restart `python -m memorymaster.mcp_server` | keep — OVER 800, split plan below; module path shim mandatory (scripts grep process name) |
| mcp_path_policy.py | surfaces | 1 | 2026-05-17 | 146 | MCP top-level import | keep |
| mcp_usage.py | surfaces | 1 | 2026-06-10 | 42 | CLI mcp-usage-report | keep |
| dashboard.py | surfaces | 1 | 2026-06-10 | 1581 | EP memorymaster-dashboard, CLI run-dashboard | keep — OVER 800, split plan below |
| dashboard_auth.py | surfaces | 1 | 2026-05-17 | 196 | via dashboard | keep |
| dashboard_integrity.py | surfaces | 1 | 2026-06-10 | 85 | via dashboard | keep |
| operator.py | surfaces | 1 | 2026-05-12 | 1453 | CLI run-operator, SCRIPT e2e_operator | keep — OVER 800, split plan below |
| operator_queue.py | surfaces | 1 | 2026-06-10 | 330 | via operator | keep |
| turn_schema.py | surfaces | 1 | 2026-03-07 | 128 | via operator | keep |
| setup_hooks.py | surfaces | 0 | 2026-06-10 | 415 | EP memorymaster-setup, scripts/setup-hooks.py, installs hooks+MCP+schtask | keep |
| metrics_exporter.py | surfaces | 1 | 2026-04-10 | 459 | CLI export-metrics, SCRIPT operator_metrics | keep |
| session_tracker.py | surfaces | 1 | 2026-06-10 | 129 | CLI sessions (cli_handlers_curation:668) | keep |

### bridges/ (13)

| Module | Subpkg | Fan-in | Last | LOC | Surface | Verdict — evidence |
|---|---|---|---|---|---|---|
| dream_bridge.py | bridges | 3 | 2026-06-10 | 786 | CLI dream-seed/ingest/sync/clean; dream-sync hook (installed, unregistered) | keep — shim mandatory |
| db_merge.py | bridges | 1 | 2026-06-10 | 425 | CLI merge-db; windows-hermes-sync.ps1 schtasks AM/PM (PRODUCTION); openclaw-sync.sh VM cron | keep — production sync path |
| delta_sync.py | bridges | 1 | 2026-06-10 | 175 | CLI export-delta; hermes sync scripts (PRODUCTION) | keep |
| atlas_contract.py | bridges | 2 | 2026-05-07 | 381 | CLI atlas-version + Atlas cmd family | keep — single-commit but CLI-wired; audit Atlas family for activity post-P2 |
| atlas_claim_extractor.py | bridges | 1 | 2026-05-07 | 162 | CLI extract-atlas-claims | keep |
| action_extractor.py | bridges | 1 | 2026-05-07 | 212 | CLI propose-actions/action-proposals | keep |
| action_exporters.py | bridges | 1 | 2026-05-07 | 91 | CLI export-actions | keep |
| media_processing.py | bridges | 2 | 2026-05-07 | 199 | CLI media-retry/transcribe/ocr | keep |
| media_providers.py | bridges | 1 | 2026-05-07 | 256 | CLI media cmds | keep |
| connectors/__init__.py | bridges | 0 | 2026-05-07 | 2 | package init | keep |
| connectors/whatsapp.py | bridges | 1 | 2026-05-07 | 235 | CLI import-whatsapp | keep — single-commit but CLI-wired |
| qmd_bridge.py | bridges | 0 | 2026-03-21 | 132 | tests/test_qmd_bridge.py + docs only | **merge -> bridges/ (or drop)** — zero importers, zero CLI/MCP/hook/script; created 2026-03-21 (< 6mo, kill blocked by dormancy rule) |
| federated_graphify.py | bridges | 0 | 2026-04-27 | 154 | tests + docs only; MCP federated_query and CLI federated-query route through service.federated_query (mcp_server.py:1534), NOT this module despite its docstring | **merge -> core/service.federated_query (fold or drop)** — shipped-but-never-wired; not dormant (2026-04-27) so kill blocked |

## Final subpackage map

```
memorymaster/
  __init__.py, __main__.py, config_templates/hooks/* (8)        # root, unchanged (10)
  core/      models, lifecycle, service, config, policy, security, scope_utils,
             observability, retry, spool, llm_provider, key_rotator, access_control,
             webhook, hook_log, plugins[merge]                   # 16
  stores/    storage, _storage_{shared,lifecycle,read,schema,sources,write_claims},
             postgres_store, store_factory, schema, snapshot,
             migrations/{__init__,runner,0001..0007}             # 19
  recall/    context_hook, retrieval, recall_fusion, recall_tokenizer, query_cache,
             query_expansion, query_classifier, context_optimizer, embeddings,
             qdrant_backend, qdrant_recall_fallback, verbatim_store, verbatim_recall,
             llm_rerank, graph_store, claim_edges                # 16
  govern/    steward, steward_classifier, steward_features, llm_steward, llm_budget,
             scheduler, review, feedback, rl_trainer, auto_resolver, conflict_resolver,
             candidate_dedupe, claim_verifier, contradiction_probe, verbatim_cleanup,
             jobs/* (16)                                         # 31
  knowledge/ wiki_engine, wiki_freshness, wiki_similarity, wiki_suggest,
             wiki_validate[merge], vault_{bases,curator,exporter,linter,log,synthesis},
             vault_query_capture[merge], entity_{extractor,graph,registry},
             rules, rule_miner, rule_export, transcript_miner, auto_extractor,
             closets, daily_notes, skill_evolver[merge]          # 23
  surfaces/  cli, cli_handlers_{basic,curation,integrity}, cli_helpers, mcp_server,
             mcp_path_policy, mcp_usage, dashboard, dashboard_auth, dashboard_integrity,
             operator, operator_queue, turn_schema, setup_hooks, metrics_exporter,
             session_tracker                                     # 17
  bridges/   dream_bridge, db_merge, delta_sync, atlas_contract, atlas_claim_extractor,
             action_extractor, action_exporters, media_processing, media_providers,
             connectors/{__init__,whatsapp}, qmd_bridge[merge],
             federated_graphify[merge]                           # 13
```
Total: 145. [merge] = 6 merge candidates (kill blocked for all by dormancy rule).

## Over-800 split plans (13 files)

| File | LOC | Split plan |
|---|---|---|
| postgres_store.py | 2613 | `stores/postgres/{store.py (facade+CRUD), schema.py (DDL, parity with _storage_schema), read.py (queries/FTS), lifecycle.py (status transitions)}` — mirror the sqlite mixin split; storage-parity rule demands both stores change together |
| context_hook.py | 2185 | `recall/{hook_entry.py (recall/observe CLI + query_for_task), gather.py (FTS/vector/graph candidate gather), scoring.py (fusion+rank), render.py (context block formatting)}` — keep `memorymaster/context_hook.py` shim re-exporting `query_for_task`/`recall` (production hook imports it) |
| service.py | 1905 | `core/service/{__init__.py (MemoryService facade — API stable), ingest.py, query.py, cycle.py, federated.py}` — facade preserves 14 script callers + hooks |
| steward.py | 1739 | `govern/steward/{__init__.py, proposals.py, arbiter.py}` + existing steward_classifier/steward_features move under govern/ |
| mcp_server.py | 1586 | `surfaces/mcp/{server.py (FastMCP bootstrap+main), tools_query.py, tools_ingest.py, tools_admin.py}` — MUST keep `memorymaster/mcp_server.py` shim: swap scripts + MCP registration target `python -m memorymaster.mcp_server` |
| dashboard.py | 1581 | `surfaces/dashboard/{app.py (routes+main), views.py, api.py}` + dashboard_auth/dashboard_integrity join the subdir; keep EP path via pyproject update |
| cli_handlers_basic.py | 1566 | split by command family: `handlers_db.py` (init/migrate/snapshot/qdrant), `handlers_atlas.py` (~Atlas third), `handlers_lifecycle.py` (run-cycle/decay/compact/dedup), `handlers_query.py` (query/context/recall-analysis) |
| operator.py | 1453 | `surfaces/operator/{loop.py, actions.py, render.py}` + operator_queue + turn_schema |
| llm_steward.py | 1074 | `govern/{llm_cycle.py (main cycle+EP main), validators.py}`; move KeyRotator/DEFAULT_COOLDOWN_SECONDS to core/key_rotator.py (breaks llm_provider<->llm_steward cycle); replace llm_steward.py:499 `create_store` lazy import with injected store (breaks govern->stores cycle) |
| cli_handlers_curation.py | 1062 | `handlers_wiki.py`, `handlers_entities.py`, `handlers_mining.py` (mine/dream/sessions) |
| _storage_sources.py | 1006 | borderline (4 commits, stable): defer; if split, read/write halves `sources_read.py`/`sources_write.py` |
| wiki_engine.py | 964 | `knowledge/wiki/{absorb.py, cleanup.py, breakdown.py}` — keep `memorymaster/wiki_engine.py` shim (steward-cycle schtask imports it) |
| _storage_schema.py | 867 | borderline: extract DDL string constants to `stores/sqlite/ddl.py`, keep logic; fold 11-LOC schema.py in |

## Migration order (least-coupled subpackage first)

Each phase = `git mv` + back-compat shim modules at old paths + pyproject EP update where needed + full gate (`pytest -q`, `ruff check`, `run-cycle` smoke, recall-hook smoke).

- **Phase 0 — pre-work (no moves):** (a) decide the 6 merge candidates with operator; (b) break the 10-module SCC with 2 cuts: move the `absorb_single_claim` trigger out of lifecycle.py:40 (core->knowledge edge) and invert llm_steward.py:499 store_factory dependency via injection (govern->stores edge); (c) move KeyRotator out of llm_steward into key_rotator.py; (d) write the shim policy for the 13 externally-referenced module paths: service, models, security, llm_provider, spool, rule_miner, verbatim_store, _storage_shared, wiki_engine, dream_bridge, hook_log, context_hook, mcp_server (hooks live OUTSIDE the repo in ~/.claude/hooks; schtasks + ps1 scripts reference these paths directly).
- **Phase 1 — bridges/** (fan-in <= 3, pure leaves; shims: dream_bridge, db_merge path stays CLI-routed). Verify: hermes-sync schtasks dry-run.
- **Phase 2 — surfaces/** (zero internal fan-in; everything imports INTO it, nothing imports FROM it except cli_helpers fan-in 4 intra-surfaces). Update pyproject [project.scripts]; shim mcp_server + re-run memorymaster-setup to refresh installed hook/MCP registrations.
- **Phase 3 — knowledge/** (reached from core only via the Phase-0-cut lazy edge; shim wiki_engine). Verify: steward-cycle schtask manual run.
- **Phase 4 — recall/** (shim context_hook + verbatim_store; production recall hook test on a live prompt).
- **Phase 5 — govern/** (jobs + steward family; EP memorymaster-steward update; shim llm_steward path only if any script references it — none found beyond EP).
- **Phase 6 — stores/** (highest fan-in: _storage_shared 40 — every remaining module updated in one sweep; shim _storage_shared for steward-cycle hook). Verify: smoke_postgres.ps1 + WAL/integrity CLI.
- **Phase 7 — core/** (models 38, security 14, service 12 — imported by every other subpackage, so it moves LAST when all internal importers already use new paths; shims for service/models/security/llm_provider/spool/hook_log remain permanently until installed hooks are regenerated).

Rationale: order is strictly ascending by inbound coupling (bridges/surfaces ~0-4 internal fan-in -> knowledge/recall -> govern -> stores/core 38-40), so each phase only rewrites imports inside already-moved code plus shims, never ahead of itself.

## Cross-checked merge candidates (kill blocked for all — none dormant)

| Module | import-graph analyst | external-surface analyst | activity analyst | Verdict |
|---|---|---|---|---|
| skill_evolver | GENUINE ORPHAN, zero refs | STRONGEST KILL CANDIDATE, doc-only, `evolve-skills` cmd never wired | last 2026-06-10, 3 commits — NOT dormant | merge/decision |
| plugins | tests-only importer | zero live consumers of entry-point group | single-commit 2026-03-21 — NOT dormant (< 6mo) | merge -> core |
| qmd_bridge | tests-only importer | tests + docs only | single-commit 2026-03-21 — NOT dormant | merge -> bridges |
| federated_graphify | tests-only; live path uses service.federated_query | dead despite 'MCP helper' docstring | single-commit 2026-04-27 — NOT dormant | merge -> core/service |
| wiki_validate | tests-only importer | hook reimplements inline, does not import it; has own `__main__` | 2026-06-01 — NOT dormant | merge -> knowledge (or wire hook) |
| vault_query_capture | tests-only importer | documented `--save-to-vault` flag never wired | 2026-06-01 — NOT dormant | merge -> knowledge |
