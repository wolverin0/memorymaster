# Compatibility and shim retirement policy

Inventory frozen: 2026-07-14.

`MemoryService` remains the public compatibility facade while bounded services
are extracted behind it. R4.2 starts with the Atlas/media/action integration
service and dashboard application models. The next ratchets are telemetry and
lifecycle, then stewardship and ingestion, with policy-dense retrieval last.
No placeholder collaborator counts as an extraction.

## Root import shims

The repository currently retains the root-level import family introduced by
the package-layer move, including `memorymaster.service`. Those shims forward
to `memorymaster.core`, `stores`, `recall`, `govern`, `knowledge`, `bridges`, or
`surfaces`; new code must import the canonical path.

Existing shims remain available through the 4.5.x line. Their earliest removal
date is 2026-09-30 and removal is permitted only in v5.0 or later.

## Removal gate

A shim may be deleted only when all of the following are true:

1. Repository imports and clean-wheel tests use the canonical path.
2. Release notes name the old path, replacement path, and removal release.
3. Available import/deprecation telemetry shows no supported consumer, or the
   owner records an explicit compatibility decision.
4. A major-version test proves the old import fails intentionally and the new
   import preserves the supported behavior.

No new root shim may be added without an owner, canonical replacement,
introduction release, planned removal release, and test. Version-source drift
is intentionally owned by R4.4; this policy does not choose a version value.
