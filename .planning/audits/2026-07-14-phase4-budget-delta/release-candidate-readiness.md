# Local Release-Candidate Readiness

## Decision

**LOCAL RC READY; PRODUCTION/EXTERNAL RELEASE BLOCKED.**

The branch is locally releasable at each atomic package checkpoint and passes
the Phase 4 convergence boundary. It must not be pushed, tagged, published, or
deployed until the prioritized external blockers are resolved or explicitly
accepted by their authorized owners.

## Candidate identity

| Item | Identity |
|---|---|
| Version | 4.4.1 |
| Wheel | `memorymaster-4.4.1-py3-none-any.whl` |
| Wheel SHA-256 | `3C10C02DB34636CE919A6BE6845AC571CB78B7A8F89332F007A1260FEB96B23A` |
| Sdist | `memorymaster-4.4.1.tar.gz` |
| Sdist SHA-256 | `B4C56EEFAFA37484C10FEEFDC9893773940BC3B0D51A920F359DB251CF1B6C1B` |
| Local image | `memorymaster:phase4-local` |
| Local image ID | `sha256:d3069774253f1d6b72d443ae8771dad98b0be6c51f78592d363ce7dc827584a2` |

These are local evidence artifacts, not approved immutable release assets.
The verified tag workflow must rebuild a candidate, record its hashes, verify
the downloaded bytes, promote without rebuilding, and publish those exact
bytes only after approval.

## Passed locally

- Full non-ML and isolated ML gates.
- Browser governance, accessibility, mobile, and failure semantics.
- Project Ruff, generated truth, workflow contracts, syntax/import, and diff
  hygiene.
- Isolated wheel install and runtime/CLI/MCP smoke outside the source tree.
- Compose fail-closed rendering and a disposable local-image/database
  health/readiness smoke.

## Not proven locally

See `remaining-blockers.md` and the canonical
`external-actions-required.md`. These gaps prevent a production-ready claim.
