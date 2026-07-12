# MemoryMaster Phase 1 Budget Audit Delta

**Date:** 2026-07-12

**Baseline:** `.planning/audits/2026-07-10-baseline/audit-report.md`

**Scheduler:** `.planning/REMEDIATION-EXECUTION-V3-BUDGET.md`

**Scope:** MM-SEC-01, MM-SEC-02 Phase 1 containment, MM-SEC-03,
MM-SEC-04, MM-OPS-01, and MM-OPS-04 Phase 1 defaults only.

## Verdict

Phase 1 repository convergence is complete under the V3 budget contract. No
reproducible Critical/High regression introduced by the Phase 1 branch remains
unresolved. External runtime, product-data, credential-rotation, advisory, and
image-scan evidence is explicitly blocked rather than treated as passing.

This was not a 13-domain reaudit. Phases 2-4 and their findings remain open.

## Finding delta

| Finding | Baseline | Phase 1 disposition | Delta |
|---|---|---|---|
| MM-SEC-01 | Critical / open | `BLOCKED-EXTERNAL` | R1.1-R1.2 repository enforcement is complete and fail-closed. Real two-role Postgres proof and brownfield repair authority remain external; the Team/Postgres profile stays disabled. |
| MM-SEC-02 | Critical / open | `IN-PROGRESS` overall; Phase 1 containment complete | All Qdrant payload-read adapters remain quarantined. Maintenance clients now use scoped API keys, verified TLS, and no redirects. R2.1 governed candidate rehydration remains backlog and semantic reads stay disabled. |
| MM-SEC-03 | High / open | `BLOCKED-EXTERNAL` | Persisted-field gateways and aggregate-only inventory are repository-complete. The authorized live read-only inventory found legacy unscannable/truncated material and no configured Qdrant; no cleanup/redaction was authorized. |
| MM-SEC-04 | High / open | `RESOLVED` | Steward, compact-summary, verbatim, bridge, feedback, and integration writes are covered by the shared persisted-envelope policy and adversarial matrices. |
| MM-OPS-01 | Critical / open | `BLOCKED-EXTERNAL` | Fixed deployment credentials were removed; Compose is fail-closed and ports are private. Historical rotation/recreation and an external port probe remain operator actions. |
| MM-OPS-04 | High / open | `IN-PROGRESS` overall; Phase 1 defaults complete | Loopback bindings, immutable digests, Qdrant auth/TLS, redirect denial, clean-wheel/SBOM binding, and scanner policy are committed. Approved runtime images and R3.4 entrypoint/readiness work remain outstanding. |

## Implemented evidence

- `a3e3824` — hardened bridge persistence transport.
- `702b59d` — aggregate-only legacy sensitivity inventory and fail-closed
  SQLite/artifact/spool/Qdrant accounting.
- `a858419` — fail-closed secret inputs, private bindings, and digest-only
  deployment defaults.
- `b71e18f` — bound repository/history/dependency/image/SBOM supply-chain
  policy and validator.
- `9b3e16c` — separated Qdrant clients, API-key/CA propagation, remote HTTPS
  enforcement, redirect denial, TLS-enabled Compose, and HTTPS Helm default.
- `8d80abb` — routed the new inventory reader through the canonical read-only
  SQLite helper and corrected final compatibility fixtures.
- `132e5d0` — aligned the required ML fixture with the secure transport.

The Qdrant TLS deployment settings follow Qdrant's documented `enable_tls`,
certificate, and key configuration, and the transport rejects plaintext remote
endpoints before client/network construction.

## Verification boundary

| Gate | Result |
|---|---|
| Supply-chain focused package | 65 passed; Ruff/format/diff checks passed before `b71e18f`. |
| Qdrant focused non-ML package | 130 passed, 24 deselected, 2 intentional xfails before `9b3e16c`. |
| Independent security review | Two High Qdrant findings (plaintext authenticated defaults and cross-origin redirect credential forwarding), zero Critical; both corrected in `9b3e16c`. |
| Final non-ML run | Single V3 run: 3,940 passed, 70 skipped, 95 deselected, 11 intentional xfails, 10 failures. The failures were nine stale transport test adapters plus one real canonical-connection regression. |
| Failure reconciliation | Exact 10-test failure set passed; focused R1.4 integrity gate passed 46 with 1 environment skip after `8d80abb`. Per V3, the 15-minute full suite was not rerun. This is compositional evidence, not a claim that a second full-suite invocation passed. |
| Collection | 4,126 tests collected. |
| Required Qdrant ML | 38 passed on the final fixture state. |
| Ruff | Final `memorymaster/` plus changed scripts/tests passed. |
| Compose | Missing required inputs failed closed; complete synthetic key/digest/certificate inputs rendered successfully. No containers were started. |
| Clean wheel | Wheel built from `git archive HEAD`, installed into a fresh venv, and imported in isolated mode. SHA-256: `d3a98b7ed6db406a1080b7cb23aad4277845caa6019211d8a2600070a89c39ed`. |
| SBOM | CycloneDX 1.6 root identity and SHA-256 binding validated against that exact wheel. |
| GitNexus | Impact checks were LOW or unavailable for newly added/unindexed private helpers; staged change detection was run before each commit. The index was refreshed with embeddings after commits. |

## External blockers

- Known unsuppressed Gitleaks history evidence remains 40 potential findings
  across 10 commits and 7 files. No suppression or pass claim was added. A
  security owner must classify and rotate before any approved history action.
- Strict project and `mcp,qdrant,security` dependency audits exceeded the
  single 15-minute advisory-service cap and were terminated. They are
  `BLOCKED-EXTERNAL/advisory-timeout`, not passing.
- Docker and Docker Scout were available, but no approved local immutable
  MemoryMaster, Qdrant, or Ollama images existed. V3 forbade pulls/builds solely
  for scanning, so image CVE evidence is `BLOCKED-EXTERNAL`.
- Helm was unavailable; no Kubernetes render/runtime claim is made.
- Disposable authenticated/TLS Qdrant and two-role Postgres targets were not
  available. Fake/local tests passed, but real-service parity remains external.
- The live inventory was read-only and did not change the database. Cleanup,
  redaction, rebuild, migration, backlog, and retention operations remain
  unauthorized.
- MemoryMaster MCP recall was attempted before the transport decision but the
  MCP transport was closed; no live-DB fallback was used.

See `external-actions-required.md` for owners and required evidence.

## Backlog boundary

- R2.1: governed Qdrant candidate-ID rehydration and unified retrieval planner.
- R2.2-R2.5: lifecycle authority, entity convergence, capture/budget/retention,
  and mock-evidence removal.
- R3.1-R3.5: performance, setup truth, service readiness, recovery,
  observability, and privacy operations.
- R4.1-R4.4: modularity, UX/accessibility, and generated release truth.
- Medium review note: validate Qdrant transport configuration before loading a
  potentially downloadable embedding model in the standalone indexer.

None of these items was executed or accepted by this goal.

## Rollback

- Keep semantic Qdrant reads disabled; revert `9b3e16c`/`132e5d0` only with
  Qdrant integrations disabled.
- Revert R1.4 commits only while affected ingestion/bridge paths are disabled;
  never restore a sensitivity bypass.
- Revert deployment/supply-chain commits only to another fail-closed private,
  immutable, authenticated configuration.
- Postgres schema rollback requires a verified backup or forward repair; do
  not edit immutable migrations or mutate live data under this record.

## Stop condition

The latest targeted verification has zero unresolved new Critical/High Phase 1
regressions. All six authorized rows are resolved for repository scope or
validly blocked/in-progress at their explicit Phase 2-4 or external boundary.
Stop here; do not begin another audit loop.
