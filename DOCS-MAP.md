<!-- doc-head: DOCS-MAP - one line per tracked document (283) with its purpose and whether it is current -->
<!-- Covers: every git-tracked *.md file in the repo, grouped by area; status legend below the header. -->
<!-- Read when: before opening any other doc, to find the authoritative one and skip stale or historical records. -->
<!-- Authority: AGENTS.md (agent rules), README.md (product), ROADMAP.md (sole roadmap), docs/development.md (verification). -->
<!-- CURRENT* = current but names files/commands no longer in the tree (listed in Notes); fix the reference, do not trust it blindly. -->
<!-- Rebuilt 2026-11-04 by a docs audit; earlier verdicts and last-change dates kept where the previous map had them. -->
<!-- /doc-head -->
# DOCS-MAP - memorymaster

Status legend: **CURRENT** in force · **CURRENT\*** in force but cites missing files/commands · **GENERATED** rebuilt by a script, do not hand-edit · **HISTORICAL** dated record or evidence, not instructions · **SUPERSEDED** replaced (see Notes) · **ARCHIVED** kept for links only · **ABANDONED** dead end.

Counts: ABANDONED 9, ARCHIVED 12, CURRENT 130, CURRENT* 15, GENERATED 3, HISTORICAL 102, SUPERSEDED 12

Missing references ignore gitignored local paths (`artifacts/` outputs, `obsidian-vault/`, `.claude/_backups/`, `.claude/worktrees/`) and user-level `.claude/settings*.json`/hooks, which are expected to exist only on an operator machine.

## Root

| File | Purpose | Status | Last change | Notes |
|---|---|---|---|---|
| AGENTS.md | Shared agent entrypoint for changing MemoryMaster (all agents; CLAUDE.md and GEMINI.md import it) | CURRENT | 2026-11-04 |  |
| ARCHITECTURE.md | Memory Reliability System (v3.2) - Architecture | SUPERSEDED | 2026-10-25 | v3.2 spec; current map is docs/architecture.md |
| AUDIT-T2070.md | AUDIT-T2070 — security audit and tests | CURRENT | 2026-11-03 |  |
| CHANGELOG.md | Release history; 4.9.0 release candidate (live TypeSafe Jev decisions with an RL-grade decision ledger, weekly-review... | CURRENT* | 2026-11-03 | names missing: `memorymaster/jobs/daydream_ingest.py` (moved), `.planning/audits/2026-09-23-weekly-claude-review/REPORT.md`, `docs/longmemeval-results.md` (+2 more) |
| CLAUDE.md | Claude Code entrypoint; imports AGENTS.md (shared instructions) and adds only Claude-specific notes | CURRENT | 2026-11-04 |  |
| COMPETITOR_ANALYSIS.md | Competitor analysis: Cognee, claude-mem, and Serena | CURRENT | 2026-10-25 |  |
| CONTRIBUTING.md | Contributing to MemoryMaster | CURRENT | 2026-11-02 |  |
| CREDITS.md | Credits, Prior Art & Re-Survey Watchlist | CURRENT | 2026-10-25 |  |
| DOCS-MAP.md | This map: one line per tracked document with purpose and currency verdict | CURRENT | 2026-11-04 |  |
| GATES.md | Useful-memory milestone executable acceptance and measured evidence | CURRENT | 2026-10-25 |  |
| GEMINI.md | Gemini CLI entrypoint; imports AGENTS.md and adds Gemini notes | CURRENT | 2026-10-25 |  |
| INSTALLATION.md | Installation Guide | CURRENT | 2026-10-25 |  |
| README.md | Product overview, quick start, public operations, source-checkout install and tests | CURRENT | 2026-11-04 |  |
| ROADMAP-v3.2.md | Historical v3.2 roadmap | SUPERSEDED | 2026-10-25 | stub; see ROADMAP.md |
| ROADMAP.md | Lane backlog tracker at the top (2026-11-02); sole roadmap; explicit MCP authors restored, client telemetry separated... | CURRENT* | 2026-11-03 | names missing: `.planning/audits/2026-09-23-graphrag/REPORT.md`, `.planning/audits/2026-09-23-weekly-claude-review/REPORT.md` |
| USER_GUIDE.md | MemoryMaster User Guide | HISTORICAL | 2026-10-25 | last dated 2026-05; README + docs/handbook.md are current |
| external-actions-required.md | External Actions Required | HISTORICAL | 2026-10-25 | last dated 2026-08-31 |
| monitoring.md | OmniClaude monitoring profile for this repo (graphify snapshot of 2026-04-10) | HISTORICAL | 2026-10-25 | 2026-04-10 snapshot |
| omniclaude-coder-handoff.md | OmniClaude — Handoff to Implementer Session | HISTORICAL | 2026-10-25 | one-off handoff for another project (OmniClaude); names missing: `memorymaster query-memory`, `.claude/skills/monitoring-setup/SKILL.md`, `docs/OMNICLAUDE_M0.md` (+2 more) |
| omniclaude-monitoring-prompt.md | OmniClaude Monitoring Setup Prompt | HISTORICAL | 2026-10-25 | one-off prompt for another project (OmniClaude) |
| roadmapres.md | Historical April 2026 roadmap | SUPERSEDED | 2026-10-25 | stub; see ROADMAP.md |

## Guides and references (docs/)

| File | Purpose | Status | Last change | Notes |
|---|---|---|---|---|
| docs/AGENT-INSTALL.md | MemoryMaster — Agent Install Prompt | CURRENT | 2026-10-25 |  |
| docs/INTEGRATING.md | Integrating an agent with MemoryMaster — the 3-Beat Contract | CURRENT* | 2026-10-25 | names missing: `.planning/P4-AGENTS-CONTRACT.md` |
| docs/MCP-TOOLS.md | MCP Tools Reference | CURRENT | 2026-10-25 |  |
| docs/README.md | MemoryMaster docs index | CURRENT | 2026-10-25 |  |
| docs/ROADMAP.md | Historical documentation roadmap | SUPERSEDED | 2026-10-25 | stub; see ROADMAP.md |
| docs/architecture.md | MemoryMaster Architecture | CURRENT* | 2026-10-25 | names missing: `memorymaster/jobs/calibration.py` (moved), `memorymaster/jobs/compact_summaries.py` (moved), `memorymaster/jobs/compactor.py` (moved) (+11 more) |
| docs/atlas-api-contract-v1.md | Atlas Inbox API/CLI Contract - v1.5.0 | CURRENT* | 2026-10-25 | names missing: `memorymaster dashboard` |
| docs/atlas-inbox-v1-technical-spec.md | Atlas Inbox V1 Technical Spec | CURRENT* | 2026-10-25 | names missing: `memorymaster/connectors/whatsapp.py` (moved) |
| docs/cli-cookbook.md | MemoryMaster CLI Cookbook | CURRENT | 2026-10-28 |  |
| docs/code-navigation.md | Shared CLI navigation with bounded live symbol retrieval | CURRENT | 2026-10-25 |  |
| docs/compatibility.md | Compatibility and shim retirement policy | CURRENT | 2026-10-25 |  |
| docs/cookbook.md | MemoryMaster Gotcha Cookbook | CURRENT | 2026-10-25 |  |
| docs/cross-project-patterns.md | Cross-Project Federation Contract | CURRENT | 2026-10-25 |  |
| docs/daydream-integration.md | Daydream Integration | CURRENT | 2026-10-25 |  |
| docs/deployment_profiles.md | Deployment Profiles (D4) | CURRENT | 2026-10-25 |  |
| docs/development.md | Scoped verification, Windows hook commands, positional trust and native OpenCode loading | CURRENT | 2026-11-02 |  |
| docs/e2e-candidate-deployment.md | September 8 installation and rollback procedure; executed delivery has a separate current record | CURRENT | 2026-11-04 |  |
| docs/enabling-v2-systems.md | Enabling the 2026-04-23 v2 Systems | CURRENT | 2026-10-25 |  |
| docs/env-reference.md | Environment variable reference | GENERATED | 2026-10-27 |  |
| docs/generated/release-truth.md | Generated release truth from executable package surfaces | GENERATED | 2026-11-03 |  |
| docs/governed-skills.md | Governed skills with catalog-first recall and optional Jev selection (decision surface skills) | CURRENT | 2026-10-25 |  |
| docs/handbook.md | MemoryMaster Handbook | CURRENT | 2026-11-02 |  |
| docs/hooks-telemetry.md | Cross-CLI telemetry, independent claim authorship and disposable diagnosis | CURRENT | 2026-10-26 |  |
| docs/human-review.md | Optional human judgments after agent-owned technical memory review | CURRENT | 2026-10-25 |  |
| docs/incident_drill_checklist.md | Incident Drill Checklist (D6) | CURRENT | 2026-10-25 |  |
| docs/incident_drill_evidence_template.md | Incident Drill Evidence and Signoff Template (D7) | CURRENT | 2026-10-25 |  |
| docs/integration/hermes-agent-brief.md | MemoryMaster ↔ Hermes-Agent Integration Brief | SUPERSEDED | 2026-10-25 |  |
| docs/integration_playbooks.md | Integration Playbooks (D5) | CURRENT | 2026-10-25 |  |
| docs/jev-decisions.md | User guide for TypeSafe Jev decisions and the decision ledger (memorymaster.decisions, 4.9.0) | CURRENT | 2026-10-25 |  |
| docs/operations.md | MemoryMaster Operations | CURRENT | 2026-10-31 |  |
| docs/project-projections.md | AO-3 configuration, incremental source cursor and local acceptance | CURRENT | 2026-10-25 |  |
| docs/public-v1.md | Public v1 facade with opt-in skills and graph observations | CURRENT | 2026-10-25 |  |
| docs/retrieval-governance-upgrades-2026-06-24.md | Retrieval & Governance Upgrades — implementation plan (2026-06-24) | CURRENT* | 2026-10-25 | names missing: `benchmark/longmemeval_s_qa.json` |
| docs/security_supply_chain.md | Supply-chain security checks | CURRENT | 2026-10-25 |  |
| docs/troubleshooting.md | Troubleshooting | CURRENT | 2026-10-28 |  |
| docs/workflow-intelligence.md | Workflow Intelligence v1 implementation and shadow-rollout contract | CURRENT | 2026-10-25 |  |

## Decisions (docs/adr/)

| File | Purpose | Status | Last change | Notes |
|---|---|---|---|---|
| docs/adr/0001-local-first-single-database.md | Authoritative MemoryMaster data remains one local-first database | CURRENT | 2026-10-25 |  |
| docs/adr/0002-atlas-inbox-extends-memorymaster-primitives.md | 0002 Atlas Inbox Extends MemoryMaster Primitives | CURRENT | 2026-10-25 |  |
| docs/adr/0003-external-connectors-own-fetching.md | 0003 External Connectors Own Fetching | CURRENT | 2026-10-25 |  |
| docs/adr/0004-action-gateways-require-approved-proposals.md | 0004 Action Gateways Require Approved Proposals | CURRENT | 2026-10-25 |  |
| docs/adr/0005-atlas-contract-uses-semver.md | 0005 Atlas Contract Uses SemVer | CURRENT | 2026-10-25 |  |
| docs/adr/0006-sensitivity-filter-boundary.md | 0006 Sensitivity Filter Boundary | CURRENT | 2026-10-25 |  |
| docs/adr/0007-secrets-use-external-stores-and-boot-guards.md | 0007 Secrets Use External Stores and Boot Guards | CURRENT | 2026-10-25 |  |
| docs/adr/0008-wiki-read-layer-and-contradiction-callouts.md | 0008 Wiki Read Layer and Contradiction Callouts | CURRENT | 2026-10-25 |  |
| docs/adr/0009-schema-fallback-is-not-migration-success.md | 0009 Schema Fallback Is Not Migration Success | CURRENT | 2026-10-25 |  |
| docs/adr/0010-candidate-dedupe-runs-before-validation.md | 0010 Candidate Dedupe Runs Before Validation | CURRENT | 2026-10-25 |  |
| docs/adr/0011-recall-ranking-features-stay-measured-and-gated.md | 0011 Recall Ranking Features Stay Measured and Gated | CURRENT | 2026-10-25 |  |
| docs/adr/0012-readme-stays-concise-handbook-holds-operator-depth.md | 0012 README Stays Concise, Handbook Holds Operator Depth | CURRENT | 2026-10-25 |  |
| docs/adr/0013-context-loading-is-two-phase.md | 0013 Context Loading Is Two Phase | CURRENT | 2026-10-25 |  |
| docs/adr/0014-wiki-articles-auto-promote-after-n-successful-validations.md | 0014 Wiki Articles Auto-Promote After N Successful Validations | CURRENT* | 2026-10-25 | names missing: `memorymaster/jobs/validator.py` (moved) |
| docs/adr/0015-governed-universal-capture-lineage.md | ADR 0015: Governed universal capture lineage | CURRENT | 2026-10-25 |  |
| docs/adr/0016-workflow-analytics-uses-disposable-sidecar.md | Accepted boundary for the disposable Workflow Intelligence sidecar | CURRENT | 2026-10-25 |  |
| docs/adr/2026-04-23-steward-v2-classifier.md | ADR: steward v2 classifier — feature engineering beat threshold tuning | CURRENT | 2026-10-25 |  |
| docs/adr/2026-04-23-tokenizer-v2-idf-fix.md | ADR: tokenizer v2 — df=0 IDF penalty + stem/synonym recovery | CURRENT | 2026-10-25 |  |
| docs/adr/README.md | ADR index through the Workflow Intelligence sidecar decision | CURRENT | 2026-10-25 |  |

## Agent rules and skills (.claude/)

| File | Purpose | Status | Last change | Notes |
|---|---|---|---|---|
| .claude/_curation_log_2026-04-18.md | Log of the 2026-04-18 .claude/ rules curation | HISTORICAL | 2026-10-25 |  |
| .claude/rules/claims-lifecycle.md | Claims Lifecycle Rules | CURRENT | 2026-10-25 |  |
| .claude/rules/mcp-server.md | MCP Server Rules | CURRENT | 2026-10-25 |  |
| .claude/rules/python/coding-style.md | Python Coding Style | CURRENT | 2026-10-25 |  |
| .claude/rules/python/hooks.md | Python Hooks | CURRENT | 2026-10-25 |  |
| .claude/rules/python/patterns.md | Python Patterns | CURRENT | 2026-10-25 |  |
| .claude/rules/python/security.md | Python Security | CURRENT | 2026-10-25 |  |
| .claude/rules/python/testing.md | Python Testing | CURRENT | 2026-10-25 |  |
| .claude/rules/sensitivity-filter.md | Sensitivity Filter Rules | CURRENT* | 2026-10-25 | names missing: `tests/test_sensitivity_filter.py` |
| .claude/rules/storage-parity.md | Storage Parity Rules | CURRENT | 2026-10-25 |  |
| .claude/skills/gitnexus/gitnexus-cli/SKILL.md | Use when the user needs to run GitNexus CLI commands like analyze/index a repo, check status, clean the index, genera... | CURRENT | 2026-10-25 |  |
| .claude/skills/gitnexus/gitnexus-debugging/SKILL.md | Use when the user is debugging a bug, tracing an error, or asking why something fails. Examples: "Why is X failing?\... | CURRENT | 2026-10-25 |  |
| .claude/skills/gitnexus/gitnexus-exploring/SKILL.md | Use when the user asks how code works, wants to understand architecture, trace execution flows, or explore unfamiliar... | CURRENT | 2026-10-25 |  |
| .claude/skills/gitnexus/gitnexus-guide/SKILL.md | Use when the user asks about GitNexus itself — available tools, how to query the knowledge graph, MCP resources, grap... | CURRENT | 2026-10-25 |  |
| .claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md | Use when the user wants to know what will break if they change something, or needs safety analysis before editing cod... | CURRENT | 2026-10-25 |  |
| .claude/skills/gitnexus/gitnexus-refactoring/SKILL.md | Use when the user wants to rename, extract, split, move, or restructure code safely. Examples: "Rename this function... | CURRENT | 2026-10-25 |  |
| .claude/skills/mm-audit/SKILL.md | Deep adversarial code audit via a dynamic workflow. Fan-out finders across correctness/concurrency/security/perf/cont... | CURRENT | 2026-10-25 |  |
| .claude/skills/mm-feature-rnd/SKILL.md | Feature R&D via a dynamic workflow. Mine a codebase's roadmap, scaffolded/gated features, ADRs, and novel ideas for n... | CURRENT | 2026-10-25 |  |
| .claude/skills/mm-mine-corrections/SKILL.md | Mine your own correction history for recurring rules. A dynamic workflow harvests user-correction turns from the Memo... | CURRENT | 2026-10-25 |  |

## Plans and specs (.planning/)

| File | Purpose | Status | Last change | Notes |
|---|---|---|---|---|
| .planning/ATLAS-LLM-EXTRACTOR-SPEC.md | Spec - LLM Typed-Entity Atlas Extractor | CURRENT | 2026-10-25 |  |
| .planning/AUTORESEARCH-PROGRAM-2026-08-03.md | MemoryMaster autoresearch program | CURRENT | 2026-10-25 |  |
| .planning/COMPILED-USER-PROFILE-V1.md | Compiled Gemini profile contract and scheduled-launcher activation discrepancy | CURRENT | 2026-10-25 |  |
| .planning/DREAMING-SOURCE-STEWARD-V1.md | Source-aware Dreaming steward deployed as 4.8.7; fleet reconnection remains outstanding | CURRENT | 2026-10-25 |  |
| .planning/ENGINEERING-CONSOLIDATION-2026-09-05.md | 4.8.7 deployed; 88-action review, restored backup and remaining fleet/evaluation limits | CURRENT | 2026-10-25 |  |
| .planning/GRAPH-OBSERVATIONS-V1.md | PPR-7 shipped contract with scheduled-launcher activation discrepancy | CURRENT | 2026-10-25 |  |
| .planning/HANDOFF-2026-08-26.md | HANDOFF 2026-08-26 — rama fix/mm-rulings-spend-and-freeze-20260824 | HISTORICAL | 2026-10-25 |  |
| .planning/HANDOFF-2026-08-29.md | HANDOFF 2026-08-29 — sesión al 86% de contexto | HISTORICAL | 2026-10-25 |  |
| .planning/HANDOFF-2026-08-31.md | HANDOFF — memorymaster, 2026-08-31 | HISTORICAL | 2026-10-25 |  |
| .planning/HERMES-SCOPE-SKILLS-INTEGRATION-2026-08-07.md | Hermes P5 repair5 verifier passed; PR #189 open | CURRENT | 2026-10-25 |  |
| .planning/INERT-SIGNALS-ROADMAP.md | Inert-signal remediation — roadmap | HISTORICAL | 2026-10-25 | dated 2026-08-18; ROADMAP.md is the sole roadmap |
| .planning/JEV-LIVE-4.9.0.md | Implementation contract for MemoryMaster 4.9.0 — live TypeSafe Jev decisions with an RL-grade decision ledger, plus t... | CURRENT | 2026-10-25 |  |
| .planning/LOCALFS-SPEC.md | Spec — Local-Filesystem Intelligence Layer (PR1) | CURRENT | 2026-10-25 |  |
| .planning/MEMORYMASTER-DREAMING-V1.md | Native Dreaming contract; configured providers and run-scoped timeout fallback | CURRENT | 2026-10-25 |  |
| .planning/MEMORYMASTER-INGEST-INCIDENT-2026-07-16.md | MemoryMaster ingest incident — 2026-07-16 | CURRENT | 2026-10-25 |  |
| .planning/ONBOARDING-SPEC.md | Spec — Install / Onboarding Rework | CURRENT | 2026-10-25 |  |
| .planning/OPEN-WORK.md | OPEN-WORK — lista viva de trabajo pendiente de MemoryMaster | HISTORICAL | 2026-10-25 | worklist frozen at 2026-08-21; ROADMAP.md is the sole roadmap |
| .planning/OPERATING-ENVELOPE-2026-07-11.md | MemoryMaster Operating Envelope | CURRENT | 2026-10-25 |  |
| .planning/P1-RELIABILITY-SPEC.md | P1 Reliability Build Spec — WAL-Discipline + Scheduled Integrity (v3.29 target) | CURRENT* | 2026-10-25 | names missing: `memorymaster/jobs/fk_repair.py` (moved), `memorymaster/jobs/integrity.py` (moved), `memorymaster/jobs/qdrant_reconcile.py` (moved) (+1 more) |
| .planning/P2-CENSUS.md | P2 Census Scorecard — MemoryMaster module-by-module | CURRENT | 2026-10-25 |  |
| .planning/P3-INTAKE-POLICY-SPEC.md | P3 Intake Policy — Design Spec | CURRENT | 2026-10-25 |  |
| .planning/PAPER-RADAR-REVIEW-2026-08-08.md | MemoryMaster paper-radar review - 2026-08-08 | CURRENT | 2026-10-25 |  |
| .planning/PAPER-RESEARCH-IMPLEMENTATION-2026-08-08.md | Implementation ledger; PPR-1 through PPR-6 complete | CURRENT | 2026-10-25 |  |
| .planning/PHASE0-RED-TEST-MATRIX.md | Phase 0 Adversarial Red-Test Matrix | CURRENT* | 2026-10-25 | names missing: `tests/test_setup_profile_verification.py` |
| .planning/PROGRAM-LOG.md | MM v4 Consolidation Program — Execution Log | SUPERSEDED | 2026-10-25 |  |
| .planning/R1.4-LEGACY-SENSITIVITY-INVENTORY.md | R1.4 legacy sensitivity inventory | CURRENT | 2026-10-25 |  |
| .planning/REMAINING-PHASES-V1-BUDGET.md | MemoryMaster Remaining Phases — Budget Execution V1 | SUPERSEDED | 2026-10-25 |  |
| .planning/REMEDIATION-EXECUTION-V2.md | MemoryMaster Remediation Execution V2 | CURRENT | 2026-10-25 |  |
| .planning/REMEDIATION-EXECUTION-V3-BUDGET.md | MemoryMaster Phase 1 — Budget Execution V3 | CURRENT | 2026-10-25 |  |
| .planning/REMEDIATION-OPTIMIZATION-PLAN-2026-07-10.md | MemoryMaster Remediation & Optimization Plan | CURRENT | 2026-10-25 |  |
| .planning/ROADMAP-CIERRE-2026-08.md | ROADMAP DE CIERRE — memorymaster, agosto 2026 | HISTORICAL | 2026-10-25 | names missing: `.claude/CLAUDE.md`, `.claude/rules/git-workflow.md` |
| .planning/RULE-LINEAGE-DRAIN-2026-09-01.md | Minado de reglas / linaje — estado al detenerse, 2026-09-01 | HISTORICAL | 2026-10-25 |  |
| .planning/SHARED-MCP-SERVER-T0726.md | Design + measured POC for ONE shared MemoryMaster MCP server for all sessions (T-0726) | CURRENT* | 2026-11-01 | names missing: `integrations/shared-mcp/windows/install.ps1` |
| .planning/T-1661-SCOPE-RESOLUTION.md | T-1661 shared Git project scope resolution and read-only census | CURRENT | 2026-10-25 |  |
| .planning/UPSTREAM-COMPARISON-2026-09-05.md | Upstream research refresh with 17 pinned snapshots and bounded improvement proposals | CURRENT | 2026-10-25 |  |
| .planning/UPSTREAM-HARDENING-IMPLEMENTATION-2026-08-03.md | MemoryMaster upstream hardening implementation — 2026-08-03 | CURRENT | 2026-10-25 |  |
| .planning/UPSTREAM-PRIOR-ART-DELTA-2026-08-03.md | MemoryMaster upstream prior-art delta — 2026-08-03 | CURRENT | 2026-10-25 |  |
| .planning/UPSTREAM-SNAPSHOTS-2026-09-05.json | Machine-readable companion data | CURRENT | 2026-09-05 | |
| .planning/USEFUL-MEMORY-V1.md | Installed recall repair and September 28 daily/weekly evidence; quality limits | CURRENT* | 2026-10-25 | names missing: `audits/2026-09-23-weekly-claude-review/REPORT.md` |
| .planning/VNEXT-BASELINE-2026-07-27.md | MemoryMaster vNext pre-change baseline | CURRENT | 2026-10-25 |  |
| .planning/VNEXT-GOVERNED-CAPTURE-SPEC.md | MemoryMaster vNext governed capture implementation specification | CURRENT | 2026-10-25 |  |
| .planning/WEEKLY-ACCEPTANCE-2026-08-24.md | Revisión semanal de aceptación — 2026-08-24 | HISTORICAL | 2026-10-25 |  |
| .planning/WORKTREES-PODA-2026-08-24.md | Inventario de los 28 worktrees — lista de poda con evidencia | HISTORICAL | 2026-10-25 | names missing: `docs/credits-refresh` |
| .planning/WORKTREES-PODA-2026-08-29.md | Poda de worktrees ejecutada — 2026-08-29 | HISTORICAL | 2026-10-25 |  |
| .planning/audit-remediation-ledger.md | Audit Remediation Ledger | CURRENT | 2026-10-25 |  |
| .planning/codebase/ARCHITECTURE.md | Architecture | CURRENT | 2026-10-25 |  |
| .planning/codebase/CONCERNS.md | Triaged 2026-08-17 against c832df4 (v4.7.6). Edit body => update this. | CURRENT | 2026-10-25 |  |
| .planning/codebase/CONVENTIONS.md | Coding Conventions | CURRENT | 2026-10-25 |  |
| .planning/codebase/INTEGRATIONS.md | External Integrations | CURRENT | 2026-10-25 |  |
| .planning/codebase/STACK.md | Technology Stack | CURRENT | 2026-10-25 |  |
| .planning/codebase/STRUCTURE.md | Codebase Structure | CURRENT | 2026-10-25 |  |
| .planning/codebase/TESTING.md | Testing Patterns | CURRENT | 2026-10-25 |  |
| .planning/evidence/T-1433/AC3-comparison.md | T-1433 measured post-trim similarity versus index and on-demand reading | HISTORICAL | 2026-10-31 |  |
| .planning/evidence/T-1433/AC3-two-judge-replay.md | T-1433 AC3 two-judge replay of index + on-demand reading versus similarity injection | HISTORICAL | 2026-10-31 |  |

## Dated audits (.planning/audits/)

| File | Purpose | Status | Last change | Notes |
|---|---|---|---|---|
| .planning/audits/2026-07-10-baseline/audit-report.md | MemoryMaster Technical Due-Diligence Baseline | CURRENT* | 2026-10-25 | names missing: `memorymaster/core/plugins.py` |
| .planning/audits/2026-07-12-phase1-budget-delta/audit-delta.md | MemoryMaster Phase 1 Budget Audit Delta | CURRENT | 2026-10-25 |  |
| .planning/audits/2026-07-13-phase2-budget-delta/audit-delta.md | MemoryMaster Phase 2 Budget Audit Delta | CURRENT | 2026-10-25 |  |
| .planning/audits/2026-07-13-phase2-budget-delta/pr-draft.md | Draft PR — Governed MemoryMaster Phase 2 Core | ABANDONED | 2026-10-25 |  |
| .planning/audits/2026-07-14-phase3-budget-delta/audit-delta.md | Phase 3 Budget Audit Delta — 2026-07-14 | CURRENT | 2026-10-25 |  |
| .planning/audits/2026-07-14-phase3-budget-delta/pr-draft.md | Draft PR — Phase 3 performance and operational readiness | CURRENT | 2026-10-25 |  |
| .planning/audits/2026-07-14-phase4-budget-delta/audit-delta.md | Phase 4 Budget Audit Delta — 2026-07-14 | CURRENT | 2026-10-25 |  |
| .planning/audits/2026-07-14-phase4-budget-delta/pr-release-draft.md | Draft PR and Release Description | CURRENT | 2026-10-25 |  |
| .planning/audits/2026-07-14-phase4-budget-delta/production-release-approval-request.md | Ready-to-Paste Production/Release Approval Request | CURRENT | 2026-10-25 |  |
| .planning/audits/2026-07-14-phase4-budget-delta/production-release-verification.md | MemoryMaster 4.5.0 Production Release Verification | CURRENT | 2026-10-25 |  |
| .planning/audits/2026-07-14-phase4-budget-delta/release-candidate-readiness.md | Local Release-Candidate Readiness | CURRENT | 2026-10-25 |  |
| .planning/audits/2026-07-14-phase4-budget-delta/remaining-blockers.md | Prioritized Remaining-Blocker Ledger | CURRENT | 2026-10-25 |  |
| .planning/audits/2026-07-14-phase4-budget-delta/rollback-plan.md | Rollback Plan | CURRENT | 2026-10-25 |  |
| .planning/audits/2026-07-27-vnext-governed-capture/audit-delta.md | MemoryMaster vNext governed capture audit delta | CURRENT | 2026-10-25 |  |
| .planning/audits/2026-08-04-autoresearch-convergence/audit-delta.md | MemoryMaster autoresearch convergence audit delta - 2026-08-04 | CURRENT | 2026-10-25 |  |
| .planning/audits/2026-08-07-hermes-scope-skills/audit-delta.md | Hermes P5 repair5 verifier passed; PR #189 open | CURRENT | 2026-10-25 |  |
| .planning/audits/2026-09-07-e2e-review/AI-REREVIEW-13.md | AI re-review rejects all 13 emitted statements as standalone current memory | CURRENT* | 2026-10-25 | names missing: `docs/EVE-ROADMAP.md` |
| .planning/audits/2026-09-07-e2e-review/IMPLEMENTATION.md | Source candidate delivered and selection v2 installed; current live evidence linked below | CURRENT | 2026-10-25 |  |
| .planning/audits/2026-09-07-e2e-review/LIVE-DEPLOYMENT.md | September 20 semantic candidate deployment; September 8 selection evidence retained below | CURRENT | 2026-10-25 |  |
| .planning/audits/2026-09-07-e2e-review/REPORT.md | Completed bounded MemoryMaster E2E review; fixes proposed, not implemented | HISTORICAL | 2026-10-25 | names missing: `scripts/install-windows-operational-review.ps1` |
| .planning/audits/2026-09-07-e2e-review/TASK.md | Completed MemoryMaster E2E review and prioritized proposals | HISTORICAL | 2026-10-25 |  |
| .planning/audits/2026-09-07-e2e-review/USEFUL-SELECTION.md | Useful-selection source and provider evidence; subsequent live deployment recorded separately | CURRENT | 2026-10-25 |  |
| .planning/audits/2026-09-27-recall-journey/REPORT.md | Governed recall journey and complete evidence delivery | HISTORICAL | 2026-10-25 |  |
| .planning/audits/2026-09-28-code-intelligence/INDEPENDENT-REVIEW.md | Independent read-only review of the 20260928 shared code-navigation setup | HISTORICAL | 2026-10-25 | names missing: `memorymaster/capture/jev_shadow.py` |
| .planning/audits/2026-09-28-code-intelligence/REPORT.md | Installed cross-client code navigation and bounded retrieval evidence | HISTORICAL | 2026-10-25 |  |
| .planning/audits/2026-10-04-autoresearch/BASELINES.md | Measured offline baselines (2026-10-04) | HISTORICAL | 2026-10-25 |  |
| .planning/audits/2026-10-04-autoresearch/DECISIONS.md | Decisions per campaign (2026-10-04) | HISTORICAL | 2026-10-25 |  |
| .planning/audits/2026-10-04-autoresearch/EXTERNAL-CONTROLLERS.md | External autoresearch references (read 2026-10-04) | HISTORICAL | 2026-10-25 |  |
| .planning/audits/2026-10-04-autoresearch/INSTRUMENTS.md | Existing instruments, gaps, and proof that the evaluators reject bad results | HISTORICAL | 2026-10-25 |  |
| .planning/audits/2026-10-04-autoresearch/MANIFESTS.md | Campaign manifests: the first three | HISTORICAL | 2026-10-25 | names missing: `scripts/autoresearch.py` |
| .planning/audits/2026-10-04-autoresearch/REPORT.md | MemoryMaster autoresearch, first run (2026-10-04) | HISTORICAL | 2026-10-25 |  |
| .planning/audits/2026-10-04-autoresearch/catalog-A-B-O-P.md | Campaigns A, B, O, P: recall performance | HISTORICAL | 2026-10-25 |  |
| .planning/audits/2026-10-04-autoresearch/catalog-C-D-F-N.md | Campaigns C, D, F, N: retrieval quality, hooks, Jev, cost | HISTORICAL | 2026-10-25 |  |
| .planning/audits/2026-10-04-autoresearch/catalog-E-G-H-I-K.md | Campaigns E, G, H, I, K: capture, budget, dreaming, steward, compaction | HISTORICAL | 2026-10-25 |  |
| .planning/audits/2026-10-04-autoresearch/catalog-J-L-M.md | Campaigns J, L, M: graph observations, profile, dashboard, recovery | HISTORICAL | 2026-10-25 |  |
| .planning/audits/2026-10-13-utility-ac4/README.md | T-1343 AC4 timestamp-window correction and verification | HISTORICAL | 2026-10-25 |  |

## Other

| File | Purpose | Status | Last change | Notes |
|---|---|---|---|---|
| benchmarks/README.md | Benchmarks | CURRENT | 2026-10-25 |  |
| integrations/hermes-memorymaster/README.md | Hermes MemoryMaster provider | CURRENT | 2026-10-25 |  |
| memorymaster/config_templates/claude-md-append.md | Template appended to users' CLAUDE.md by memorymaster-setup (MCP read/write rules) | CURRENT | 2026-10-25 |  |
| memorymaster/config_templates/codex-agents-md-append.md | Template appended to users' Codex AGENTS.md by memorymaster-setup | CURRENT | 2026-10-25 |  |
| reports/T-0121-INTEGRITY-SAFETY-AUDIT.md | T-0121 read-only claims sanitization and support-lineage audit | HISTORICAL | 2026-10-25 |  |
| scripts/RUNBOOK-wal-truncate.md | RUNBOOK — Supervised WAL TRUNCATE checkpoint | HISTORICAL | 2026-10-25 | 2026-06-09 runbook; verify against current WAL tooling before use |

## Archive (docs/archive/)

| File | Purpose | Status | Last change | Notes |
|---|---|---|---|---|
| docs/archive/AUDIT-2026-04-09.md | MemoryMaster Technical Audit — 2026-04-09 | ABANDONED | 2026-10-25 |  |
| docs/archive/IMPROVEMENT_PLAN.md | MemoryMaster Improvement Plan | ABANDONED | 2026-10-25 | names missing: `memorymaster init`, `memorymaster/jobs/compactor.py` (moved), `memorymaster/jobs/deterministic.py` (moved) |
| docs/archive/dedup-key-audit-2026-05-11.md | Dedup Key Prefix-Collision Audit - 2026-05-11 | SUPERSEDED | 2026-10-25 |  |
| docs/archive/longmemeval-results.md | LongMemEval-S retrieval results | ARCHIVED | 2026-10-25 |  |
| docs/archive/mcp-tool-audit.md | MCP Tool Compliance Audit | ARCHIVED | 2026-10-25 |  |
| docs/archive/recall-architecture-2026-04-23.md | Recall architecture (2026-04-23) | ARCHIVED | 2026-10-25 |  |
| docs/archive/scenarios/lifecycle-edge-cases-2026-05-11.md | Lifecycle Edge Cases - 2026-05-11 | ARCHIVED | 2026-10-25 | names missing: `memorymaster/jobs/compact_summaries.py` (moved), `memorymaster/jobs/compactor.py` (moved), `memorymaster/jobs/decay.py` (moved) (+3 more) |
| docs/archive/security-audit-2026-05-11.md | MemoryMaster Security Audit - 2026-05-11 | ARCHIVED | 2026-10-25 | names missing: `docs/security-audit/findings.md`, `memorymaster/dashboard_helpers`, `memorymaster/dashboard_helpers.py` (+1 more) |
| docs/archive/security-audit/findings.md | Detailed Security Findings | ARCHIVED | 2026-10-25 |  |
| docs/archive/security-audit/owasp-coverage.md | OWASP 2021 Coverage | ARCHIVED | 2026-10-25 |  |
| docs/archive/security-audit/threat-model.md | Security Audit Threat Model | ARCHIVED | 2026-10-25 | names missing: `memorymaster/dashboard_helpers.py`, `memorymaster/dashboard_helpers_extra.py` |
| docs/archive/sensitivity-audit-2026-05-11.md | Sensitivity-Filter Leak Audit - 2026-05-11 | ARCHIVED | 2026-10-25 | names missing: `memorymaster/redact.py` (moved) |
| docs/archive/storage-parity-divergence.md | SQLite <-> Postgres Schema Parity Divergence | ARCHIVED | 2026-10-25 |  |
| docs/archive/storage-parity-report-2026-05-10.md | Storage Parity Report - 2026-05-10 | SUPERSEDED | 2026-10-25 |  |
| docs/archive/v315-experiments/E01-results.md | EXPERIMENT v315-E01 - Enable vector signal in LongMemEval bench harness | SUPERSEDED | 2026-10-25 |  |
| docs/archive/v315-experiments/E02-results.md | EXPERIMENT v315-E02 - RRF fusion in query_rows | ABANDONED | 2026-10-25 |  |
| docs/archive/v315-experiments/E04-results.md | EXPERIMENT v315-E04 - Session-diversity reranker | ABANDONED | 2026-10-25 |  |
| docs/archive/v315-experiments/E05-results.md | EXPERIMENT v315-E05 - W_LEX sweep | ABANDONED | 2026-10-25 |  |
| docs/archive/v315-experiments/E06-results.md | EXPERIMENT v315-E06 - Gemini cross-encoder rerank | SUPERSEDED | 2026-10-25 |  |
| docs/archive/v315-experiments/E08-results.md | EXPERIMENT v315-E08 - FTS5 porter stemming | ABANDONED | 2026-10-25 |  |
| docs/archive/v316-experiments/S1-results.md | S1 Results: Unify Retrieval Weight Constants | ABANDONED | 2026-10-25 |  |
| docs/archive/v316-experiments/S2-results.md | V316-S2 — RRF as tiebreaker for near-tie candidates | ABANDONED | 2026-10-25 |  |
| docs/archive/v316-roadmap.md | V3.16+ improvement roadmap | ARCHIVED | 2026-10-25 |  |
| docs/archive/v318-experiments/E01-results.md | V318-E01 — per-question-type retrieval profile for single-session-preference | ARCHIVED | 2026-10-25 | names missing: `docs/longmemeval-results.md` |
| docs/archive/v320-backlog.md | V3.20.0 backlog — Phase 1 + Phase 2 pendings | SUPERSEDED | 2026-10-25 | names missing: `docs/longmemeval-results.md` |
| docs/archive/wiki-frontmatter-audit-2026-05-12.md | Wiki Frontmatter Audit - 2026-05-12 | GENERATED | 2026-10-25 |  |

## Local evidence reports (artifacts/)

| File | Purpose | Status | Last change | Notes |
|---|---|---|---|---|
| artifacts/2026-10-27-t1860-raw-fallback.md | T-1860: raw-transcript fallback (retrieval-only measurement) | HISTORICAL | 2026-10-27 |  |
| artifacts/T-1704-r3-evidence.md | T-1704 round-three alias review and executed regression evidence | HISTORICAL | 2026-10-25 |  |
| artifacts/T-1705-verification.md | T-1705 source verification of surrogate-safe recall boundaries | HISTORICAL | 2026-10-25 |  |
| artifacts/T-1794-verification.md | T-1794 source verification and retained execution failures | HISTORICAL | 2026-10-25 |  |
| artifacts/bm25-per-field-eval-2026-04-23.md | BM25 per-field weighting — eval (roadmap 1.4, 2026-04-23) | HISTORICAL | 2026-10-25 |  |
| artifacts/bm25-sweep-2026-04-23.md | BM25 k1/b sweep — 2026-04-23 | HISTORICAL | 2026-10-25 |  |
| artifacts/cognee-assessment-2026-04-24.md | Cognee vs MemoryMaster — assessment (2026-04-24) | HISTORICAL | 2026-10-25 | names missing: `docs/recall-architecture-2026-04-23.md` |
| artifacts/cynical-deletion-audit-2026-04-27.md | Cynical-deletion audit (v3.9.0 F9) | HISTORICAL | 2026-10-25 |  |
| artifacts/entity-extraction-wave3-2026-04-23.md | Entity Extraction — Wave 3 Results (#127) | HISTORICAL | 2026-10-25 | names missing: `tests/fixtures/synth_eval.db` |
| artifacts/entity-l2-backfill-2026-04-23.md | Entity Layer-2 backfill — dry-run report | HISTORICAL | 2026-10-25 |  |
| artifacts/entity-new-kinds-2026-04-23.md | Roadmap 3.2 — Entity-kind expansion (Layer 1) | HISTORICAL | 2026-10-25 |  |
| artifacts/eval-expansion-2026-04-23.md | Recall Eval Expansion — 30 → 100 prompts (2026-04-23) | HISTORICAL | 2026-10-25 |  |
| artifacts/final-roadmap-2026-04-23.md | MemoryMaster — Final Roadmap (2026-04-23) | HISTORICAL | 2026-10-25 | names missing: `docs/recall-architecture-2026-04-23.md`, `scripts/backfill_entity_graph.py` |
| artifacts/graph-distance-weight-eval-2026-04-25.md | Wave §12.1 — Graph Score Distance-Weighting Eval | HISTORICAL | 2026-10-25 |  |
| artifacts/graphify-queue-2026-04-24.md | Graphify queue — legacy projects (2026-04-24) | HISTORICAL | 2026-10-25 |  |
| artifacts/harness-consolidation-2026-04-24.md | Harness Consolidation Report — roadmap 11.7 | HISTORICAL | 2026-10-25 |  |
| artifacts/harness-fallout-audit-2026-04-25.md | Harness Rewrite Fallout Audit — Wave §12.2 | HISTORICAL | 2026-10-25 |  |
| artifacts/longmemeval-2026-04-24.md | LongMemEval — MemoryMaster production-stack benchmark, 2026-04-24 | HISTORICAL | 2026-10-25 |  |
| artifacts/longmemeval-per-q-2026-04-24.md | LongMemEval — per-question DB isolation (roadmap 11.4) | HISTORICAL | 2026-10-25 |  |
| artifacts/longmemeval-rrf-2026-04-24.md | LongMemEval — RRF vs linear fusion, 2026-04-24 | HISTORICAL | 2026-10-25 |  |
| artifacts/recall-latency-2026-04-23.md | Recall latency baseline (roadmap 5.1) | HISTORICAL | 2026-10-25 |  |
| artifacts/recall-measurement-top50-2026-04-27.md | Recall measurement — top-50 GT (v3.12.0, 2026-04-27) | HISTORICAL | 2026-10-25 |  |
| artifacts/recall-measurement-v3.10-2026-04-27.md | Recall measurement — v3.10.0 (2026-04-27) | HISTORICAL | 2026-10-25 |  |
| artifacts/recall-weight-tuning-2026-04-26.md | Recall weight tuning — autoresearch B1 (2026-04-26) | HISTORICAL | 2026-10-25 |  |
| artifacts/recall-weight-tuning-N953-2026-04-27.md | Recall weight grid — precision@5 tuning | HISTORICAL | 2026-10-25 |  |
| artifacts/roadmap-v3.5.2-2026-04-26.md | Roadmap v3.5.2 — Audit + Autoresearch ship | HISTORICAL | 2026-10-25 |  |
| artifacts/roadmap-v3.6.0-2026-04-27.md | Roadmap v3.6.0 — Cleaner entity graph + bigger eval | HISTORICAL | 2026-10-25 |  |
| artifacts/roadmap-v3.9.0-2026-04-27.md | Roadmap v3.9.0 — "Steal everything good" | HISTORICAL | 2026-10-25 | names missing: `memorymaster wiki-validate` |
| artifacts/rrf-auto-gate-2026-04-24.md | RRF auto-gate heuristic — evaluation artifact | HISTORICAL | 2026-10-25 |  |
| artifacts/rrf-fusion-eval-2026-04-23.md | RRF Fusion Eval — 2026-04-23 | HISTORICAL | 2026-10-25 |  |
| artifacts/scope-queryexp-eval-2026-04-23.md | Scope-aware retrieval boost (1.2) + Query expansion (1.5) — eval on 100 real prompts | HISTORICAL | 2026-10-25 |  |
| artifacts/sensitivity-v2-refresh-2026-04-23.md | Sensitivity filter — v2 adversarial refresh (2026-04-23) | HISTORICAL | 2026-10-25 |  |
| artifacts/session-handoff-2026-04-23.md | Session Handoff — 2026-04-23 | HISTORICAL | 2026-10-25 | names missing: `docs/recall-architecture-2026-04-23.md` |
| artifacts/spec-entity-extraction-at-ingest-2026-04-23.md | Spec — Entity extraction at ingest (#127 / Wave 3) | HISTORICAL | 2026-10-25 |  |
| artifacts/spec-steward-classifier-2026-04-23.md | Spec — Calibrated classifier for steward promotion (#129) | HISTORICAL | 2026-10-25 |  |
| artifacts/spec-wiki-freshness-metric-2026-04-24.md | Spec — Wiki absorb freshness metric | HISTORICAL | 2026-10-25 |  |
| artifacts/steal-from-others-2026-04-27.md | What to steal from neighboring memory tools (2026-04-27) | HISTORICAL | 2026-10-25 | names missing: `memorymaster wiki-validate` |
| artifacts/steward-classifier-feature-audit-2026-04-23.md | Steward classifier v2 — feature audit and split-strategy postmortem | HISTORICAL | 2026-10-25 |  |
| artifacts/steward-classifier-v3-backtest-2026-04-24.md | Steward classifier — v2 vs v3 back-test | HISTORICAL | 2026-10-25 | names missing: `scripts/session_to_daily_note.py` |
| artifacts/steward-classifier-v3-multiscope-backtest-2026-04-24.md | Steward classifier — v3old vs v3new back-test (item 11.5 multi-scope) | HISTORICAL | 2026-10-25 |  |
| artifacts/steward-pareto-2026-04-22.md | Steward promotion Pareto sweep — 2026-04-22 | HISTORICAL | 2026-10-25 |  |
| artifacts/steward-redesign-notes.md | Steward/validator promotion — redesign diagnosis (2026-04-22) | HISTORICAL | 2026-10-25 | names missing: `memorymaster/jobs/validator.py` (moved) |
| artifacts/verbatim-recall-eval-2026-04-23.md | MemPalace-style verbatim recall — 30-prompt eval (2026-04-23) | HISTORICAL | 2026-10-25 |  |
| artifacts/wiki-freshness-baseline-2026-04-24.md | Wiki freshness baseline — 2026-04-24 (roadmap 11.8, Option A) | HISTORICAL | 2026-10-25 |  |

## Design debates (debates/)

| File | Purpose | Status | Last change | Notes |
|---|---|---|---|---|
| debates/001-instruction-architecture/research/research_claude.md | Claude Research Notes — Instruction Architecture | HISTORICAL | 2026-10-25 |  |
| debates/001-instruction-architecture/research/research_codex.md | Codex Research Notes | HISTORICAL | 2026-10-25 |  |
| debates/001-instruction-architecture/research/research_gemini.md | Research Notes: Unified Agent Instruction Architecture Evaluation | HISTORICAL | 2026-10-25 | names missing: `.claude/CLAUDE.md`, `.claude/rules/gitnexus.md` |
| debates/001-instruction-architecture/rounds/r001_claude.md | Claude — Round 1 | HISTORICAL | 2026-10-25 | names missing: `.claude/rules/gitnexus.md` |
| debates/001-instruction-architecture/rounds/r001_codex.md | R001_codex.md | HISTORICAL | 2026-10-25 |  |
| debates/001-instruction-architecture/synthesis.md | Debate Synthesis: Unified Agent Instruction Architecture | HISTORICAL | 2026-10-25 | names missing: `.claude/rules/gitnexus.md` |
| debates/002-project-setup-v2/rounds/r001_claude.md | Claude — R1: Problems Found | HISTORICAL | 2026-10-25 |  |
| debates/002-project-setup-v2/rounds/r001_codex.md | R001_codex.md | HISTORICAL | 2026-10-25 |  |
| debates/002-project-setup-v2/rounds/r001_gemini.md | R001_gemini.md | HISTORICAL | 2026-10-25 |  |
| debates/002-project-setup-v2/rounds/r002_claude.md | Claude — R2: Discussion | HISTORICAL | 2026-10-25 |  |
| debates/002-project-setup-v2/rounds/r002_codex.md | R002_codex.md | HISTORICAL | 2026-10-25 |  |
| debates/002-project-setup-v2/rounds/r002_gemini.md | R002_gemini.md | HISTORICAL | 2026-10-25 |  |
| debates/002-project-setup-v2/rounds/r003_claude.md | Claude — R3: Challenges | HISTORICAL | 2026-10-25 |  |
| debates/002-project-setup-v2/rounds/r003_codex.md | R003_codex.md | HISTORICAL | 2026-10-25 |  |
| debates/002-project-setup-v2/rounds/r003_gemini.md | R003_gemini.md | HISTORICAL | 2026-10-25 |  |
| debates/002-project-setup-v2/rounds/r004_claude.md | Claude — R4: Converging | HISTORICAL | 2026-10-25 |  |
| debates/002-project-setup-v2/rounds/r004_codex.md | Codex - R4: Converging | HISTORICAL | 2026-10-25 |  |
| debates/002-project-setup-v2/rounds/r004_gemini.md | R004_gemini.md | HISTORICAL | 2026-10-25 |  |
| debates/002-project-setup-v2/rounds/r005_claude.md | Claude — R5: Final Verdict | HISTORICAL | 2026-10-25 |  |
| debates/002-project-setup-v2/rounds/r005_codex.md | R005_codex.md | HISTORICAL | 2026-10-25 |  |
| debates/002-project-setup-v2/rounds/r005_gemini.md | R005_gemini.md | HISTORICAL | 2026-10-25 |  |
| debates/002-project-setup-v2/synthesis.md | Debate 002 Complete Synthesis: /project-setup v2 | HISTORICAL | 2026-10-25 |  |
