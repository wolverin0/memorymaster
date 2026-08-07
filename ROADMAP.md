# MemoryMaster roadmap
# Covers: the authoritative post-v4.6 personal-first product sequence and explicit deferrals.
# Key terms: v4.6.0, remember, recall, forget, improve, governed claims, observation.
# Read when: choosing release scope, accepting a feature, or deciding whether work is deferred.
# Authority: this is the sole roadmap; `.planning/` specifications implement it and never replace it.
# Safety: SQLite remains authoritative, candidate promotion stays steward-owned, and live upgrades stay operator-gated.
# Updated: 2026-08-07 after native Hermes activation; its 24-hour observation is running.

## Shipped in v4.6.0

- The personal/local SQLite profile and versioned `remember / recall / forget /
  improve` Python, CLI, and MCP facade are public.
- Unified bounded capture, exact source -> evidence -> claim -> graph lineage,
  replay-safe background jobs, and the `personal-v1` ontology are implemented.
- Trusted graph traversal requires active authorized supporting claims and
  citations; candidate promotion remains steward-controlled.
- Capture Inbox, deterministic demo, clean package profiles, supply-chain
  evidence, LongMemEval gates, and comparable OAuth-backed QA are complete.

## Now

- Complete the seven-day post-activation observation at 2026-08-06 16:06 UTC
  (13:06 Argentina) and record queue, provider, duplicate, lease, task-result,
  graph-support, and trusted-recall evidence without changing live state.
- Complete the native Hermes provider observation at 2026-08-08 20:36
  Argentina time. The hidden headless check may create the PR only when scope,
  lineage, replay, outbox, task, OAuth, and gateway gates remain healthy; it
  must not tag, release, publish a package, deploy, or merge.
- Keep v4.6.0 operational while the post-release Obsidian opt-in and OpenCode
  OAuth capture fixes converge on `main` for a separately approved patch release.
- Preserve governed retrieval, lifecycle authority, scope isolation, finite
  capture budgets, and fail-closed production evidence defaults.

## Next

- Continue the bounded session-scope, native Hermes MemoryProvider, and
  governed-skill proposal program defined by
  `.planning/HERMES-SCOPE-SKILLS-INTEGRATION-2026-08-07.md`; Windows SQLite
  remains authoritative, global is never inferred, and skills require explicit
  approval. P1 session binding, P2 Hermes transport, and P3 governed skills are
  implemented and locally verified on their feature branch. Windows P4
  convergence, backup/restore, candidate scheduling, and consoleless runtime
  activation are complete. The live VM now uses the native provider with the
  legacy bridge disabled; shadow recall, scoped replay-safe capture, exact
  lineage, trusted-recall isolation, OAuth, and Telegram transport passed. The
  24-hour observation is running and is the only remaining PR gate.
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
