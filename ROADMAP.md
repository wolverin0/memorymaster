# MemoryMaster roadmap

This is the only authoritative product roadmap. Historical plans remain in Git
history and `docs/archive/`; they are evidence, not promises.

## Now

- Ship and operate the personal/local minimal profile: one SQLite database,
  private stdio MCP, and no required external database or vector service.
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

## Not planned

- Automatic live cleanup, compaction, redaction, migration, archival, retention
  deletion, or backlog mutation without an explicit operator action.
- Synthetic production evidence, silent provider fallbacks, or direct Qdrant
  truth that bypasses authoritative rehydration and governance.
- A second authoritative roadmap, another default vector database, or a
  flag-day rewrite of `MemoryService`.
- Making Postgres, Qdrant, containers, or multi-user operation a dependency of
  the personal/local minimal profile.
