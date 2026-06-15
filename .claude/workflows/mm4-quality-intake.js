export const meta = {
  name: 'mm4-quality-intake',
  description: 'MM v4 P3-WF1: intake policy as code — make source_agent mandatory, reject session-state scope floods, per-agent quotas, <=N distilled per stop. ADDITIVE governance, sensitivity filter untouched.',
  whenToUse: 'Phase 3 quality stage of the MemoryMaster v4 consolidation program. First P3 workflow — the faucet fix.',
  phases: [
    { title: 'Design', detail: 'audit the real ingest paths + draft the policy spec' },
    { title: 'Build', detail: 'one builder implements the policy chokepoint + tests' },
    { title: 'Verify', detail: 'adversarial verifier: filter intact, policy additive, no false rejects of good claims' },
  ],
}

const REPO = 'G:/_OneDrive/OneDrive/Desktop/Py Apps/memorymaster'

const SAFETY =
  `Repo: ${REPO}, branch omni/p3-quality (verify with git branch --show-current; STOP if different). ` +
  `NON-NEGOTIABLE SAFETY BOUNDARIES: ` +
  `(1) The sensitivity filter is SACRED — do not weaken, bypass, or reorder it. The intake policy runs ALONGSIDE the filter (both reject), never replaces it. ` +
  `(2) The policy is ADDITIVE: it may REJECT more claims (raise the bar) but must NEVER cause a claim that was previously rejected to now be accepted. No new bypass flags. ` +
  `(3) Every policy rule is configurable + has a SAFE DEFAULT and must be testable in isolation. ` +
  `(4) Do NOT touch the live memorymaster.db (production). Tests use tmp DBs. ` +
  `(5) The working tree IS production for background tasks (editable install) — keep it importable + the ingest path working at all times; the scheduled steward/hooks ingest mid-build. ` +
  `(6) NEVER git commit/push. (7) Full suite green (~12 min, run it, report real tail) + ruff clean before declaring done. ` +
  `Post-restructure layout: ingest lives in memorymaster/core/service.py (MemoryService.ingest); MCP ingest in memorymaster/surfaces/mcp_server.py; hooks in memorymaster/config_templates/hooks/. The sensitivity filter is in memorymaster/core/security.py. Read the ACTUAL code before changing — verify every claim before building.`

phase('Design')
const DESIGN_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['ingest_chokepoints', 'current_source_agent_handling', 'proposed_rules', 'risk_notes', 'spec_path'],
  properties: {
    ingest_chokepoints: { type: 'array', items: { type: 'string' }, description: 'every path a claim enters the DB through, file:line — MCP, service.ingest, hooks, dream bridge, spool drain, db_merge' },
    current_source_agent_handling: { type: 'string', description: 'why 62% of claims have NULL source_agent today — which ingest path(s) omit it, grounded in code' },
    proposed_rules: { type: 'array', items: { type: 'string' }, description: 'each policy rule with its safe default + how a caller opts a legit exception' },
    risk_notes: { type: 'array', items: { type: 'string' }, description: 'what could break (e.g. db_merge re-ingest, spool replay, legit session-state uses) and the mitigation' },
    spec_path: { type: 'string' },
  },
}
const design = await agent(
  `${SAFETY}\n\nROLE: design the intake policy. TASKS:\n` +
  `1. Map EVERY ingest chokepoint (grep for .ingest(, INSERT INTO claims, store.create_claim, spool drain replay, db_merge) — cite file:line. The policy must live at the SINGLE narrowest chokepoint they all pass through (likely service.ingest); if some paths bypass it, name them.\n` +
  `2. Diagnose the 62%-NULL-source_agent finding: which path(s) ingest without source_agent? (baseline: dominant producer is NULL, then llm-stop-hook). \n` +
  `3. Draft policy rules with SAFE DEFAULTS: (a) source_agent REQUIRED (reject or attribute-to-'unknown'? — recommend reject for MCP/explicit, default-tag for hooks); (b) scope 'session-state*' and heartbeat-shaped claims REJECTED from the claims table (they belong in verbatim/spool, not claims — this is the watchkeeper-flood class); (c) per-source_agent rate quota per cycle/day (configurable, default generous); (d) max distilled claims per stop-hook invocation (<=3, the documented norm). \n` +
  `4. Flag risks: db_merge re-ingest must not be rejected (it carries original source_agent); spool replay goes through service.ingest so it inherits policy — verify that's OK; legit session-state consumers.\n` +
  `Write the spec to ${REPO}/.planning/P3-INTAKE-POLICY-SPEC.md. Read-only this phase — no code changes.`,
  { label: 'design-intake', phase: 'Design', schema: DESIGN_SCHEMA })

phase('Build')
const BUILD_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['status', 'files_touched', 'tests_added', 'full_suite_tail', 'ruff_clean', 'policy_is_additive', 'filter_untouched', 'notes'],
  properties: {
    status: { type: 'string', enum: ['done', 'failed'] },
    files_touched: { type: 'array', items: { type: 'string' } },
    tests_added: { type: 'array', items: { type: 'string' } },
    full_suite_tail: { type: 'string' },
    ruff_clean: { type: 'boolean' },
    policy_is_additive: { type: 'boolean', description: 'true only if you verified the policy only ever REJECTS, never newly-accepts' },
    filter_untouched: { type: 'boolean', description: 'true only if security.py sensitivity filter is byte-unchanged' },
    notes: { type: 'string' },
  },
}
const build = await agent(
  `${SAFETY}\n\nROLE: implement the intake policy per ${REPO}/.planning/P3-INTAKE-POLICY-SPEC.md (read it fully — design summary: ${JSON.stringify(design && design.proposed_rules)}).\n` +
  `Implement at the single chokepoint the design identified. Each rule configurable via env/config with safe defaults. ` +
  `Intent-anchored tests REQUIRED: (a) NULL source_agent on MCP/explicit ingest is rejected/attributed per spec; (b) a session-state/heartbeat-shaped claim is rejected from claims; (c) over-quota ingest from one source_agent is throttled; (d) >N distilled per stop is capped; (e) REGRESSION: a normal good claim still ingests; (f) REGRESSION: db_merge re-ingest of an existing claim with a real source_agent still succeeds; (g) SAFETY: a sensitive-payload claim is STILL rejected by the filter (prove the filter runs and the policy didn't shadow it). ` +
  `Report status=failed if the suite isn't green. policy_is_additive and filter_untouched must be TRUE.`,
  { label: 'build-intake', phase: 'Build', schema: BUILD_SCHEMA })

if (!build || build.status !== 'done') return { design, build }

phase('Verify')
const VERIFY_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['verdict', 'filter_intact', 'additive_confirmed', 'false_reject_risk', 'issues'],
  properties: {
    verdict: { type: 'string', enum: ['pass', 'fail'] },
    filter_intact: { type: 'boolean' },
    additive_confirmed: { type: 'boolean' },
    false_reject_risk: { type: 'string', description: 'assessment of whether legit claims could be wrongly rejected' },
    issues: { type: 'array', items: { type: 'string' } },
  },
}
const verify = await agent(
  `${SAFETY}\n\nROLE: adversarial verifier for the intake policy. Builder claims: ${JSON.stringify({ files: build.files_touched, additive: build.policy_is_additive, filter: build.filter_untouched })}.\n` +
  `AUDIT: (1) git diff security.py — is the sensitivity filter byte-unchanged? (2) Trace the ingest path: does EVERY claim still pass the filter, with the policy as an ADDITIONAL gate (not a replacement)? Write a throwaway test ingesting a fake-credential claim and assert it's STILL rejected. (3) Could the policy FALSE-REJECT legit claims? Specifically test: db_merge re-ingest, spool replay, a legit claude-session claim with source_agent set, a multi-claim stop under the cap. (4) Is the policy truly additive — find any path where it could newly-accept something previously rejected. Report pass/fail with specifics.`,
  { label: 'verify-intake', phase: 'Verify', schema: VERIFY_SCHEMA })

return { design, build, verify }
