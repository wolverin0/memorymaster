<!-- doc-head: T-1656 source verification and blocked publication -->
<!-- Covers: local acceptance, read-only census and lane publication handoff. -->
<!-- /doc-head -->
# T-1656 verification

Source commit: `5f74b4fac683a4a45e878de7cbd55f5a9984ca45` on `lane/t1656`, parent `34a0cf4`.

- Initial disposable regression on base: 3 failed, 3 passed. The observed replacement-to-stale path reproduced.
- Final affected suites: 157 passed in 34.67 seconds, including lifecycle, validator, curation drain, auto/conflict resolver, promotion policy/structure, recovery CLI and public demo.
- Ruff on all changed Python files: passed. Generated release-truth and staged whitespace checks: passed.
- Independent review: two findings repaired (atomic drain winner; specific uniqueness exceptions); re-review found no material remaining issue.
- Read-only census: 3 affected stale replacements: mm-9e4a~2, mm-aeb2~2, mm-7357~2. Their predecessors were superseded with reciprocal pointers. No live repair was applied.
- Broad non-ML attempt: interrupted after the external-output test rejected the sandbox temporary root inside the repository. An outside-root attempt was denied by the sandbox. Full suite is not claimed green.
- Optional steward/parity expansion: 211 passed, 2 failed (dashboard/MCP parity fixtures produced no proposals, with provider/model-download requests refused by the proxy). Another optional CLI expansion was stopped during provider setup; no pass is claimed for it.
- Windows pytest temp mode 0700 was inaccessible; a local launcher substituted inherited directory permissions only for temporary-directory creation. Fixtures remained disposable.
- GitNexus: missing index in this worktree; tool discovery did not expose the server. Used scoped source caller searches, staged diff and independent review.

Publication: git push and gh pr create failed through the restricted proxy. GitHub connector tree creation was rejected because approval is required and the session approval policy is never. There is no remote branch or PR from this run.

The lane can push this existing source commit and create the requested PR with `artifacts/T-1656/PR-BODY.md`. Revalidation must wait for lane GO; the commands and selectors are in that prepared body. No merge, deployment or service restart was performed.
