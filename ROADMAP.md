<!-- doc-head: sole authoritative roadmap; P5 verifier passed, PR #189 open -->
# MemoryMaster roadmap
# Covers: post-v4.6 sequence, Tencent-derived work, paper research, and deferrals.
# Key terms: Hermes, governed skills, paper radar, temporal projection, sustainability.
# Read when: choosing release scope, accepting a feature, or checking deferrals.
# Authority: sole roadmap; planning ledgers implement it and never replace it.
# Safety: SQLite authority and steward promotion remain fixed; PR #189 is open, never authorizing merge, release, deployment, or PPR-7.
<!-- /doc-head -->

## Shipped in v4.6.0

- The personal/local SQLite profile and versioned `remember / recall / forget /
  improve` Python, CLI, and MCP facade are public.
- Unified bounded capture, exact source -> evidence -> claim -> graph lineage,
  replay-safe background jobs, and the `personal-v1` ontology are implemented.
- Trusted graph traversal requires active authorized supporting claims and
  citations; candidate promotion remains steward-controlled.
- Capture Inbox, deterministic demo, clean package profiles, supply-chain
  evidence, LongMemEval gates, and comparable OAuth-backed QA are complete.

## TencentDB Agent Memory v2.0 adoption boundary

Reviewed upstream `TencentCloud/TencentDB-Agent-Memory` at commit
`fe3230f176f1bf5832fee79d12494bbc2d19a8aa` (2026-08-06). MemoryMaster adopts
useful product patterns without importing Tencent runtime code or replacing
its governed-claims authority:

| Tencent pattern | MemoryMaster decision |
|---|---|
| Session/project isolation | Adopted as explicit durable session bindings; no inferred `global`. |
| Hermes memory integration | Adopted through the native Hermes `MemoryProvider`, authenticated MCP/HTTP authority, durable replay outbox, and read-only replica fallback. |
| Reusable skill memory | Adopted as evidence-linked `personal-skill-v1` candidates, human-only promotion, immutable versions, and confirmed scoped skill recall. |
| Per-turn matched skill injection | Implemented locally as a bounded `APPROVED SKILLS` recall section; candidate, stale, and unauthorized skills are excluded. |
| Chat memory, Wiki, and graph assets | Retain MemoryMaster source/evidence/claim capture, opt-in wiki projection, and claim-supported entity graph rather than adding parallel authorities. |
| Memory Hub, loadouts, team ACLs, proxy replacement, cloud database | Deferred: no personal SQLite requirement justifies multi-user/cloud infrastructure or a second agent gateway. |

## Now

- The 2026-08-09 post-P5 observation failed closed on one missing graph job.
  Root cause: confidence-only validation changed `claims.updated_at`, while graph
  replay identity incorrectly treated every metadata update as a new revision.
- The repair is implemented and verified: scheduled Dreaming queues due work
  before processing, graph identity uses the latest transition into `confirmed`,
  and the one live job completed with capture coverage returning `ok`.
- Clean wheel `d4d9aad` is installed in the Windows P5 runtime. Two new
  reproducible builds share SHA-256 `828a327b...9ddd`; their payload matches all
  353 installed package files. A real consoleless Dreaming run returned `0`.
- Gitleaks scanned `origin/main..HEAD` with zero findings, and authoritative
  SQLite `quick_check` completed `ok` in 172.406 seconds. The previous runtime
  mismatch and skill-isolation failures were invalid line-ending and duplicate-
  fixture checks; direct wheel and distinct-fixture canaries pass.
- The observer wrapper fails nonzero unless Codex writes a fresh explicit
  success marker. The August 10 baseline interval has elapsed; no additional
  24-hour wait is required after repair4.
- Repair4 closed scope aggregation, capture lease, Hermes stateless transport,
  durable metadata, exact terminal cleanup, and live recall defects. A stale
  user environment had overridden the selected Gemini+GLM Dreaming pair with
  OpenAI models; task-bound provider arguments now prevent recurrence.
- The pinned Gemini Flash Lite plus GLM 5.2 scheduled replay passes: exit 0,
  eight extraction/consolidation/application decisions, zero errors, and one
  stale crash run recovered. Repair5 independently replays the bounded gate
  against the elapsed baseline and passes; [PR #189](https://github.com/wolverin0/memorymaster/pull/189)
  is open, while merge, release, deployment, and PPR-7 remain prohibited.
- The invalid earlier window remains incident evidence only because it included
  a VM OOM/gateway interruption and did not contain P5.
- Keep v4.6.0 operational while the post-release Obsidian opt-in and OpenCode
  OAuth capture fixes converge on `main` for a separately approved patch release.
- Preserve governed retrieval, lifecycle authority, scope isolation, finite
  capture budgets, and fail-closed production evidence defaults.

## Next

- Complete the bounded session-scope, native Hermes MemoryProvider, and
  governed-skill proposal program defined by
  `.planning/HERMES-SCOPE-SKILLS-INTEGRATION-2026-08-07.md`; Windows SQLite
  remains authoritative, global is never inferred, and skills require explicit
  approval. P1 session binding, P2 transport, P3 governed skills, and P5
  progressive approved-skill reuse are implemented, verified, and active.
  Windows snapshot/readiness gates, consoleless P5 runtime replacement, VM
  package rollback preparation, and live functional probes passed. The repair5
  bounded verifier now passes; create a PR only, with no merge or follow-on PPR-7 work.
- Improve personal/local backup guidance beyond the already verified disposable
  backup/restore and migration procedure.
- Keep semantic recall optional and disabled unless a local user deliberately
  configures a governed Qdrant/provider profile.
- Upgrade the pinned private runtime only through a separately authorized,
  snapshot-backed operator action; a public package release is not a live cutover.

### Research-derived memory sustainability program

This program turns useful research into reproducible MemoryMaster experiments;
papers are prior art, never trusted memory or implementation authority. The
first reviewed input is `arXiv:2607.26637`, *Filesystem-Based Memory for LLM
Agents: Organization, Evolution, and Sustainability*. Its useful lesson is to
measure preservation, answer quality, and total retrieval cost separately:
organization may reduce search cost without improving answers, and uncontrolled
rewriting can erase temporal, emotional, or narrative information.

- **R0 - Governed paper radar:** build a deterministic, read-only metadata
  importer for all sections of
  `VoltAgent/awesome-ai-agent-papers`, initially pinned at upstream commit
  `c8502b6acd3978a84b8b25453eda24be83088d00`. Record source revision, observed
  time, section, title, canonical arXiv ID/version, links, and upstream summary;
  deduplicate by canonical arXiv ID and DOI. Snapshot and diff additions,
  removals, retitles, duplicate IDs, broken links, and displayed-count drift.
  A refresh may update only the non-authoritative research ledger; it must not
  mutate runtime configuration, the roadmap, evidence, claims, or skills.
- **R0 review funnel:** ingest metadata for the full list without downloading
  every PDF. Score entries against governance/lifecycle, retrieval/routing,
  procedural skills, graph/evidence, evaluation/cost, reliability, privacy, and
  security. Fetch primary arXiv metadata for shortlisted items and full text
  only for a bounded review batch. Every reviewed paper receives an explicit
  `adopt`, `benchmark`, `defer`, or `reject` verdict with primary citations,
  reproducibility notes, expected benefit, implementation surface, and cost.
  The 2026-08-08 checkpoint parsed all 57 Memory & RAG records and completed
  primary-PDF result/limitation review for an 18-paper priority batch, including
  the earlier filesystem-memory paper and cross-section active-use/cost work.
  Decisions and the ordered PPR-1 through PPR-6 packages are recorded in
  `.planning/PAPER-RADAR-REVIEW-2026-08-08.md`; importer and runtime experiments
  remain unimplemented.
- **R1 - Representation-preservation benchmark:** add private, synthetic, and
  publishable fixtures covering latest-versus-superseded state, affect and
  emphasis, narrative-arc co-retrieval, ordinary factual recall, and procedural
  reuse. Score answer correctness separately from citation correctness and raw
  evidence preservation so a cited but temporally wrong answer cannot pass.
  The offline PPR-1 checkpoint now provides eight publishable synthetic cases,
  a five-profile prediction contract, independent answer/citation/tool scores,
  exact parameter-provenance checks, and deterministic failure attribution.
  Product-profile baselines and behavior changes remain later gated work.
- **R2 - Explicit consumer-aware recall projections:** compare governed claims
  plus bounded evidence for strong consumers, concise task guidance for smaller
  consumers, lifecycle timelines for temporal/high-stakes questions, and
  confirmed skills plus warnings for procedural tasks. The caller selects a
  versioned profile; MemoryMaster must not infer a weaker trust mode or silently
  change lifecycle and scope rules. The offline PPR-3 checkpoint now defines
  explicit low/balanced/high/temporal/procedural policies and deterministic
  admission diagnostics. It remains a content-free shadow evaluator and does
  not alter the production retrieval plan.
- **R3 - Ephemeral guidance and outcome-aware skills:** synthesize cited,
  token-budgeted task guidance from confirmed authorized skills without storing
  or promoting the synthesis. Extend skill evidence with
  `success`/`failure`/`ambiguous` outcomes; failed traces may generate warnings
  but cannot reinforce a positive procedure. Promotion remains human-only. The
  offline PPR-6 checkpoint now validates content-free execution observations,
  consumer/model and tool-schema snapshots, activation/termination/validation
  results, bounded metrics, deduplication, and separate negative warnings.
  Durable outcome persistence and runtime review wiring remain gated.
- **R4 - Prioritized paper experiments:** start with query-budget routing
  (`BudgetMem`), progressive evidence sufficiency and source rehydration
  (`A2RAG`), generator-aligned evidence pruning (`Less is More for RAG`),
  intent-aware retrieval, temporal occurrence-time modeling, deterministic
  versus LLM graph extraction, and action-oriented memory evaluation
  (`Mem2ActBench`). Adopt none until a focused baseline/mutation comparison
  proves a gain on an authoritative MemoryMaster execution path. The offline
  PPR-4 checkpoint now provides bounded claim-to-evidence rehydration with
  active-source and scope/sensitivity revalidation; it is explicit evaluation
  functionality and is not a new default answer path.
- **R5 - Governed temporal projection:** the offline PPR-5 checkpoint now adds
  explicit current, latest, historical, and occurrence-time projections;
  inclusive interval overlap; citation-complete structural durative summaries;
  and bounded episode windows derived only from authorized linked evidence and
  stable source/session metadata. These rebuildable projections do not change
  schema, production ranking, or default recall.
- **R5 - Sustainability and cost gates:** measure current-versus-superseded
  errors, early-memory survival, duplication/fragmentation, citation accuracy,
  tokens, content read, tool/provider calls, latency, and cost per correct answer
  or solved task. Compare claims-only, evidence-only, claims+evidence,
  claims+approved-skills, and claims+ephemeral-guidance profiles with both a
  smaller OAuth-backed model and the stronger OAuth-backed judge. The offline
  PPR-2 checkpoint now provides a bounded aggregate-safe stage schema and a
  disposable-SQLite observer over authoritative retrieval and packing. It does
  not persist query/evidence text or enable any provider/model by itself.

Exit gates: zero secret or cross-scope leakage, zero automatic promotion,
replay-safe radar updates, primary-source traceability for every verdict, no
LongMemEval or full-QA regression beyond existing thresholds, and a measured
quality or cost win before any experimental retrieval behavior becomes a
default. Offline synthetic harness work may proceed under explicit operator
authorization; runtime experiments and activation begin only after the clean P5
observation/PR gate closes.

## Later

- Continue the measured service-facade decomposition without breaking the
  compatibility surface.
- Revisit shared multi-user/team operation only if a real use case appears;
  its Postgres, RLS, identity, deployment, and recovery gates remain deferred.
- Revisit authenticated Qdrant, immutable container images, and Kubernetes/Helm
  only for an explicitly selected semantic or hosted profile.
- Improve entity aliases and steward classification only against versioned,
  reproducible evaluation datasets.
- Expand companion integrations through the documented provider protocols and
  core-to-companion import boundary.
- Revisit hosted cloud, broad backend matrices, shared multi-user operation,
  extra SDK languages, graph-answer agents, community detection, and a full
  ontology editor only after a separately approved use case.

## Not planned

- Automatic live cleanup, compaction, redaction, migration, archival, retention
  deletion, or backlog mutation without an explicit operator action.
- Synthetic production evidence, silent provider fallbacks, or direct Qdrant
  truth that bypasses authoritative rehydration and governance.
- A second authoritative roadmap, another default vector database, or a
  flag-day rewrite of `MemoryService`.
- Making Postgres, Qdrant, containers, or multi-user operation a dependency of
  the personal/local minimal profile.
- Adding Cognee as a runtime dependency or replacing governed claims with
  graph/vector output.
- Bulk-importing paper full text into governed memory, treating curated-list
  metadata as verified evidence, or automatically implementing research claims.
- Replacing SQLite claim authority with a filesystem hierarchy, restoring the
  Obsidian projection as the read layer, or allowing an LLM to rewrite, merge,
  compact, or delete authoritative history autonomously.
