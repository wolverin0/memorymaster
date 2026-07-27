# MemoryMaster roadmap
# Covers: the authoritative personal-first MemoryMaster product sequence and explicit deferrals.
# Key terms: remember, recall, forget, improve, governed claims, capture lineage, personal-v1.
# Read when: choosing release scope, accepting a feature, or deciding whether work is deferred.
# Authority: this is the sole roadmap; `.planning/` specifications implement it and never replace it.
# Safety: SQLite remains authoritative, candidate promotion stays steward-owned, and live activation is separate.
# Updated: 2026-07-27 for governed universal capture and graph hardening.

## Now

- Ship and operate the personal/local minimal profile: one SQLite database,
  private stdio MCP, and no required external database or vector service.
- Add a small, versioned `remember / recall / forget / improve` facade over
  the governed claim lifecycle, with Python, CLI, and MCP parity.
- Capture pasted text, reference URLs, common local documents, images, and
  audio through one bounded source/evidence envelope. URL-only items remain
  `awaiting_evidence`; MemoryMaster does not fetch remote content.
- Persist replay-safe capture jobs and exact source -> evidence -> claim ->
  graph lineage. Candidate extraction may run in the existing hourly worker;
  confirmation remains exclusively steward-controlled.
- Ship the `personal-v1` ontology and make graph traversal depend on active,
  authorized supporting claims and citations.
- Add a local Capture Inbox, a disposable deterministic demo, and reproducible
  capture/lineage/graph/latency quality gates.
- Resolve only the supply-chain and publication evidence that applies to the
  local package; keep optional-profile blockers classified separately.
- Preserve governed retrieval, lifecycle authority, tenant/principal isolation,
  finite capture budgets, and fail-closed production evidence defaults.

## Next

- Improve personal/local backup guidance and verify restore on a disposable
  copy before migrating the user's active SQLite database.
- Keep semantic recall optional and disabled unless a local user deliberately
  configures a governed Qdrant/provider profile.
- Execute the verified release workflow only after explicit release approval.

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
