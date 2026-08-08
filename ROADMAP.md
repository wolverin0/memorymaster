# MemoryMaster roadmap
# Covers: the authoritative post-v4.6 product sequence, Tencent-derived improvements, and explicit deferrals.
# Key terms: v4.6.0, TencentDB Agent Memory, Hermes, governed skills, live P5, observation.
# Read when: choosing release scope, accepting a feature, or deciding whether work is deferred.
# Authority: this is the sole roadmap; `.planning/` specifications implement it and never replace it.
# Safety: SQLite remains authoritative, candidate promotion stays steward-owned, and live upgrades stay operator-gated.
# Updated: 2026-08-08 after P5 activation and replacement of the invalid observation window.

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
