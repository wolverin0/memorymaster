# Ready-to-Paste Production/Release Approval Request

```text
Request: authorize the personal/local SQLite release-candidate verification
sequence for MemoryMaster 4.4.1. Do not authorize hosted deployment or live DB
mutation.

Local status: Phase 2–4 remediation and final targeted audit converged. The
latest audit has zero new findings and no unresolved reproducible
Critical/High regression introduced by the branch. Full local non-ML, ML,
Ruff, browser/a11y, clean-wheel, generated-truth, Compose, and disposable
container/database gates passed.

Approval requested for these controlled external actions only:
1. Classify the history/Gitleaks findings and run the strict dependency audit
   against the exact minimal SQLite package extras.
2. Verify the wheel/sdist hashes and clean-install evidence produced by the
   release workflow.
3. If and only if the minimal-profile blockers are resolved or formally
   accepted, approve one
   tag-triggered verified-artifact workflow execution. Manual dispatch must
   not publish; PyPI must receive the exact promoted verified-dist bytes.

Explicitly deferred and not required for this scoped release: Postgres/team,
Qdrant/semantic, Docker/Kubernetes/Helm, hosted multi-user recovery, and
organization-wide privacy/compliance claims. Those profiles remain disabled
and must undergo their own approval sequence if revisited.

Still prohibited without a separate explicit approval: production cutover,
live migration or cleanup, redaction, archival, retention deletion, backlog
mutation, credential rotation execution, history rewrite, or product-data
mutation.

Required before package-publication approval: exact final commit, wheel/sdist
digests, minimal-profile gate evidence, clean-install/MCP smoke, and the
reviewed rollback instructions. A future hosted cutover would require a
separate request.
```
