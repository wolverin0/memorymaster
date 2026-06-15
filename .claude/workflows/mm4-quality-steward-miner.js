export const meta = {
  name: 'mm4-quality-steward-miner',
  description: 'MM v4 P3-WF3: fold correction-mining (rule_miner.mine_rules) into the steward run_cycle as a config-gated phase, inside the existing LLM budget scope. Default OFF.',
  whenToUse: 'Phase 3 quality — make correction-to-rule mining automatic per steward cycle instead of a manual skill.',
  phases: [
    { title: 'Build', detail: 'add rule_mining phase to run_cycle behind a flag, budget-aware' },
    { title: 'Verify', detail: 'adversarial: default-off, budget-respecting, sensitive-rule-dropped, no double-mining' },
  ],
}

const REPO = 'G:/_OneDrive/OneDrive/Desktop/Py Apps/memorymaster'

const SAFETY =
  `Repo: ${REPO}, branch omni/p3-steward-miner (verify with git branch --show-current; STOP if different). ` +
  `GOAL: make correction-mining automatic — call memorymaster/knowledge/rule_miner.py mine_rules(db_path, service, *, since_id, limit, batch_size, provider, reset) as a PHASE inside MemoryService.run_cycle (memorymaster/core/service.py, inside the existing "with llm_budget.cycle_scope() as budget:" block alongside extractor/validator/decay/etc.). ` +
  `mine_rules already: uses the LLM budget (raises LLMBudgetExceeded, caught by run_cycle's existing handler), persists a watermark in miner_state so it never re-mines the same verbatim rows, redacts/drops sensitive rules, ingests rules as candidate claims via service.ingest (so the intake policy + sensitivity filter both apply). It is the SAME logic as the /mm-mine-corrections skill. ` +
  `NON-NEGOTIABLE: (1) DEFAULT OFF — new config MEMORYMASTER_STEWARD_RULE_MINING (default '0'/off) gates the phase; when off, run_cycle behaves EXACTLY as today (no new LLM calls, no behavior change). When on, a conservative default per-cycle limit (MEMORYMASTER_STEWARD_RULE_MINING_LIMIT, default e.g. 25) bounds the LLM calls, and the phase runs INSIDE the cycle_scope so the global budget caps still abort it cleanly. (2) The phase must be resilient: a mine_rules failure (LLM error, etc.) must NOT crash the whole cycle — wrap it so the rest of run_cycle (decay, integrity, etc.) still completes; surface its result/abort under result['rule_mining']. (3) Do NOT weaken the sensitivity filter or intake policy. (4) Do NOT touch the live memorymaster.db. (5) Keep tree importable. (6) NEVER git commit/push. (7) Full suite green (real tail) + ruff clean before done.`

phase('Build')
const BUILD_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['status', 'files_touched', 'tests_added', 'full_suite_tail', 'ruff_clean', 'default_off_verified', 'notes'],
  properties: {
    status: { type: 'string', enum: ['done', 'failed'] },
    files_touched: { type: 'array', items: { type: 'string' } },
    tests_added: { type: 'array', items: { type: 'string' } },
    full_suite_tail: { type: 'string' },
    ruff_clean: { type: 'boolean' },
    default_off_verified: { type: 'boolean', description: 'true only if you proved run_cycle makes ZERO rule-mining LLM calls when the flag is unset' },
    notes: { type: 'string' },
  },
}
const build = await agent(
  `${SAFETY}\n\nROLE: implement the rule_mining steward phase. Read service.run_cycle + rule_miner.mine_rules first. Add the phase inside cycle_scope, config-gated default-off, conservative per-cycle limit, failure-isolated, result under result['rule_mining']. ` +
  `Intent-anchored tests REQUIRED in tests/test_steward_rule_mining.py: (1) flag OFF (default) -> run_cycle does NOT call mine_rules (assert via monkeypatch/spy, zero calls); (2) flag ON with a planted verbatim correction + stubbed extractor -> a rule candidate is mined and the result dict carries rule_mining stats; (3) a mine_rules exception does NOT crash run_cycle (other phases still run, result['rule_mining'] carries an error marker); (4) REGRESSION: existing run_cycle behavior unchanged when flag off (a normal cycle still extracts/validates/decays). ` +
  `Report status=failed if the suite isn't green. default_off_verified must be TRUE.`,
  { label: 'build-steward-miner', phase: 'Build', schema: BUILD_SCHEMA })

if (!build || build.status !== 'done') return { build }

phase('Verify')
const VERIFY_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['verdict', 'default_off', 'budget_respected', 'failure_isolated', 'sensitive_path_intact', 'issues'],
  properties: {
    verdict: { type: 'string', enum: ['pass', 'fail'] },
    default_off: { type: 'boolean' },
    budget_respected: { type: 'boolean', description: 'the phase runs inside cycle_scope so global LLM caps abort it' },
    failure_isolated: { type: 'boolean' },
    sensitive_path_intact: { type: 'boolean', description: 'mined rules still go through service.ingest -> filter + intake policy' },
    issues: { type: 'array', items: { type: 'string' } },
  },
}
const verify = await agent(
  `${SAFETY}\n\nROLE: adversarial verifier. Builder touched: ${JSON.stringify(build.files_touched)}. ` +
  `AUDIT with throwaway tests you run then delete: (1) DEFAULT-OFF — unset the flag, run a real run_cycle on a tmp DB with a spy on rule_miner.mine_rules, assert 0 calls and the cycle result is otherwise identical-shaped to before. (2) BUDGET — confirm the phase is lexically inside the cycle_scope() block and that an exhausted budget aborts it without crashing the cycle. (3) FAILURE ISOLATION — monkeypatch mine_rules to raise, assert decay/integrity phases still run. (4) SENSITIVE PATH — confirm mined rules are ingested via service.ingest (not a raw INSERT), so the filter + intake policy apply (grep rule_miner for the ingest call). Report pass/fail with specifics.`,
  { label: 'verify-steward-miner', phase: 'Verify', schema: VERIFY_SCHEMA })

return { build, verify }
