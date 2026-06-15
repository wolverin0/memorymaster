export const meta = {
  name: 'mm4-quality-filterbypass',
  description: 'MM v4 P3-WF2: close the two sensitivity-filter bypasses (dream_bridge raw insert + llm_steward cycle insert) found by the intake-policy design. Security hardening.',
  whenToUse: 'Phase 3 quality, after the intake-policy workflow surfaced the raw-INSERT filter bypasses.',
  phases: [
    { title: 'Harden', detail: 'route both raw inserts through the sensitivity filter' },
    { title: 'Verify', detail: 'adversarial: prove a credential-bearing payload is now rejected on BOTH paths' },
  ],
}

const REPO = 'G:/_OneDrive/OneDrive/Desktop/Py Apps/memorymaster'

const SAFETY =
  `Repo: ${REPO}, branch omni/p3-quality (verify with git branch --show-current; STOP if different). ` +
  `CONTEXT: the intake-policy design (.planning/P3-INTAKE-POLICY-SPEC.md) found TWO raw "INSERT INTO claims" sites that bypass the sensitivity filter — a documented invariant violation (.claude/rules/sensitivity-filter.md: "the filter MUST run on dream_bridge.py; any new ingest path — default-deny"). ` +
  `SITE 1: memorymaster/bridges/dream_bridge.py — dream_ingest() flag-off direct path raw-INSERTs claim["text"] (parsed from markdown) with no sensitivity check (note: the dream-SEED path DOES use _is_sensitive at lines ~530/599/609, but dream_INGEST's direct insert does not). ` +
  `SITE 2: memorymaster/govern/llm_steward.py (~line 803) — the steward cycle raw-INSERTs text[:200] as a NEW claim with status='confirmed', no sensitivity check, runs every cycle. ` +
  `NON-NEGOTIABLE: (1) Use the EXISTING filter — memorymaster/core/security.py is_sensitive_claim / sanitize_claim_input, the SAME one service.ingest uses; do not invent a new filter or weaken security.py (byte-unchanged). (2) On a sensitive payload: SKIP/reject the insert (default-deny) + log a redacted marker + bump a counter; never store the raw sensitive text. (3) Match each site's existing surrounding behavior otherwise — minimal diff, no refactor. (4) Do NOT touch the live memorymaster.db. (5) Keep the tree importable (editable install = production for background tasks). (6) NEVER git commit/push. (7) Full suite green (run it, real tail) + ruff clean before done.`

phase('Harden')
const BUILD_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['status', 'files_touched', 'tests_added', 'full_suite_tail', 'ruff_clean', 'filter_reused', 'security_py_unchanged', 'notes'],
  properties: {
    status: { type: 'string', enum: ['done', 'failed'] },
    files_touched: { type: 'array', items: { type: 'string' } },
    tests_added: { type: 'array', items: { type: 'string' } },
    full_suite_tail: { type: 'string' },
    ruff_clean: { type: 'boolean' },
    filter_reused: { type: 'boolean', description: 'true only if you reused core/security.py is_sensitive_claim/sanitize, not a new filter' },
    security_py_unchanged: { type: 'boolean' },
    notes: { type: 'string' },
  },
}
const build = await agent(
  `${SAFETY}\n\nROLE: wire the sensitivity filter into both raw-INSERT sites. Read both files first; find the EXACT insert and the text it stores. ` +
  `For each: before the INSERT, run the same sensitivity check service.ingest uses (read memorymaster/core/service.py to see exactly how it calls is_sensitive_claim / sanitize_claim_input on the text + subject/predicate). If sensitive: skip the insert (default-deny), record a redacted/skip event + bump an observability counter, continue. Otherwise insert as before. ` +
  `Intent-anchored tests REQUIRED in tests/test_filter_bypass_hardening.py: (1) dream_ingest with a fake-credential markdown note does NOT create a claim and the raw secret never reaches the claims table; (2) the steward cycle-insert path skips a sensitive extraction; (3) REGRESSION: a normal dream note still ingests; (4) REGRESSION: a normal steward extraction still inserts. ` +
  `Report status=failed if the suite isn't green. filter_reused + security_py_unchanged must be TRUE.`,
  { label: 'harden-bypass', phase: 'Harden', schema: BUILD_SCHEMA })

if (!build || build.status !== 'done') return { build }

phase('Verify')
const VERIFY_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['verdict', 'both_sites_filtered', 'security_py_unchanged', 'no_raw_secret_stored', 'issues'],
  properties: {
    verdict: { type: 'string', enum: ['pass', 'fail'] },
    both_sites_filtered: { type: 'boolean' },
    security_py_unchanged: { type: 'boolean' },
    no_raw_secret_stored: { type: 'string', description: 'evidence that a sensitive payload never lands in claims.text on either path' },
    issues: { type: 'array', items: { type: 'string' } },
  },
}
const verify = await agent(
  `${SAFETY}\n\nROLE: adversarial verifier. Builder touched: ${JSON.stringify(build.files_touched)}. ` +
  `AUDIT: (1) git diff security.py — byte-unchanged? (2) For EACH of the two sites, write a throwaway test that drives the real code path with a payload containing a fake credential (e.g. an API-key-shaped string) and asserts NO row with that secret exists in the claims table afterward. Run them. (3) Confirm both sites reuse core/security.py (not a reimplemented check). (4) Find any OTHER raw "INSERT INTO claims" in memorymaster/ that still bypasses the filter (grep) and report them as issues (do not fix — just report for a follow-up). Report pass/fail with specifics; delete your throwaway tests after.`,
  { label: 'verify-bypass', phase: 'Verify', schema: VERIFY_SCHEMA })

return { build, verify }
