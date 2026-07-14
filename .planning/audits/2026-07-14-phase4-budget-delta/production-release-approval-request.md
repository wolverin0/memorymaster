# Ready-to-Paste Production/Release Approval Request

```text
Request: authorize the external release-candidate verification sequence for
MemoryMaster 4.4.1. Do not authorize production cutover yet.

Local status: Phase 2–4 remediation and final targeted audit converged. The
latest audit has zero new findings and no unresolved reproducible
Critical/High regression introduced by the branch. Full local non-ML, ML,
Ruff, browser/a11y, clean-wheel, generated-truth, Compose, and disposable
container/database gates passed.

Approval requested for these controlled external actions only:
1. Resolve/classify credential-history, Gitleaks, dependency, and immutable
   release-image scan blockers.
2. Run disposable two-role Postgres RLS/parity and authenticated/TLS Qdrant
   governed-retrieval/reconciliation gates.
3. Run approved immutable-image Kubernetes/Helm and off-device/Postgres
   recovery drills.
4. Obtain product/legal privacy, consent, retention, and data-disposition
   decisions.
5. If and only if all blockers are resolved or formally accepted, approve one
   tag-triggered verified-artifact workflow execution. Manual dispatch must
   not publish; PyPI must receive the exact promoted verified-dist bytes.

Still prohibited without a separate explicit approval: production cutover,
live migration or cleanup, redaction, archival, retention deletion, backlog
mutation, credential rotation execution, history rewrite, or product-data
mutation.

Required before cutover approval: exact final commit, artifact/image digests,
external gate evidence, staging/canary health and MCP handshake, migration and
integrity evidence, telemetry/alert delivery, and the reviewed rollback
command/owner.
```
