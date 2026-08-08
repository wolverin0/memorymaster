# MemoryMaster roadmap
# Covers: the authoritative post-v4.6 sequence, Tencent-derived work, research radar, and explicit deferrals.
# Key terms: v4.6.0, Hermes, governed skills, paper radar, memory sustainability, observation.
# Read when: choosing release scope, accepting a feature, or deciding whether work is deferred.
# Authority: this is the sole roadmap; `.planning/` specifications implement it and never replace it.
# Safety: SQLite remains authoritative, candidate promotion stays steward-owned, and live upgrades stay operator-gated.
# Updated: 2026-08-08 after the offline PPR-2 sustainability-telemetry checkpoint.

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

- Observe the activated Tencent-derived P5 bundle through 2026-08-09 02:20
  Argentina time. The Windows authority and Hermes VM now run the exact audited
  wheels; live recall, rollback, consoleless tasks, outbox, and installed-wheel
  skill-isolation canaries passed at activation.
- Let the replacement hidden check close both the clean P5 24-hour window and
  the already-due seven-day v4.6 observation. The invalid earlier window
  included a VM OOM/gateway incident and did not contain P5.
- Create the PR only if scope, lineage, replay, outbox, task, provider, gateway,
  and approved-skill isolation remain healthy. The check must not tag, release,
  publish, deploy, or merge.
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
  package rollback preparation, and live functional probes passed. Complete
  the clean 24-hour observation before creating the PR.
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
  change lifecycle and scope rules.
- **R3 - Ephemeral guidance and outcome-aware skills:** synthesize cited,
  token-budgeted task guidance from confirmed authorized skills without storing
  or promoting the synthesis. Extend skill evidence with
  `success`/`failure`/`ambiguous` outcomes; failed traces may generate warnings
  but cannot reinforce a positive procedure. Promotion remains human-only.
- **R4 - Prioritized paper experiments:** start with query-budget routing
  (`BudgetMem`), progressive evidence sufficiency and source rehydration
  (`A2RAG`), generator-aligned evidence pruning (`Less is More for RAG`),
  intent-aware retrieval, temporal occurrence-time modeling, deterministic
  versus LLM graph extraction, and action-oriented memory evaluation
  (`Mem2ActBench`). Adopt none until a focused baseline/mutation comparison
  proves a gain on an authoritative MemoryMaster execution path.
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
