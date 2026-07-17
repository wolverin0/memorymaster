# MemoryMaster ingest incident — 2026-07-16

## Outcome

- Live `ingest_claim` restored.
- A consistent pre-repair backup is at
  `G:\tmp\memorymaster-pre-human-id-repair-20260716.db`.
- The backup and repaired live database both passed `PRAGMA quick_check`.

## Root cause

The live SQLite database retained the legacy global unique index
`idx_claims_human_id`. Current allocation deliberately permits the same human
ID in different governed scopes/principals, so the legacy constraint rejected
otherwise valid inserts.

The live schema was repaired transactionally to use:

- non-unique lookup index `idx_claims_human_id`;
- unique public identity by tenant, scope, and human ID; and
- unique non-public identity by tenant, scope, visibility, principal, and human
  ID.

No claims were rewritten or backfilled.

## Verification

- Scope/principal duplicate preflight: zero duplicate groups.
- Post-repair `PRAGMA quick_check`: `ok`.
- MCP ingest evidence: claim `112409`, human ID `mm-bb47~17`.
- Focused migration and identity tests: 46 passed.

## Follow-up

Full `init_db()` on an upgraded copy reaches a separate migration sequencing
conflict: obsolete migration 0009 attempts tenant-wide uniqueness before
migration 0012 replaces it with scope/principal-local identities. GitNexus
rates changes to `MigrationRunner.apply_pending` CRITICAL. Keep this as a
separate reviewed migration-compatibility package; it is not required for the
restored MCP ingest path.
