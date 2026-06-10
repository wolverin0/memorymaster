export const meta = {
  name: 'mm4-reliability-build2',
  description: 'MM v4 P1-WF2: continuation: build WAL-Discipline spec steps 8-12 (1-7 committed) in the main checkout (branch omni/p1-reliability)',
  whenToUse: 'Phase 1 build stage of the MemoryMaster v4 consolidation program. Requires .planning/P1-RELIABILITY-SPEC.md and branch omni/p1-reliability checked out.',
  phases: [{ title: 'Build', detail: '12 sequential spec steps, each self-testing; abort chain on failure' }],
}

const REPO = 'G:/_OneDrive/OneDrive/Desktop/Py Apps/memorymaster'
const SPEC = `${REPO}/.planning/P1-RELIABILITY-SPEC.md`

const RULES =
  `Repo: ${REPO}, branch omni/p1-reliability (verify with 'git branch --show-current'; if it differs, STOP and report). ` +
  `You build ONE step of the P1 WAL-Discipline spec at ${SPEC} — read YOUR step's section fully first, plus the architecture section. ` +
  `GOVERNANCE (non-negotiable): (1) VERIFY-BEFORE-BUILD — read the actual files the spec names before trusting any claim in it; if the spec mis-states current code (wrong line, function doesn't exist, already implemented), adapt to reality and say so in notes. ` +
  `(2) Minimal diffs, match surrounding style; files stay under 800 LOC — if your change would push a file over, extract a module. ` +
  `(3) Every behavior change ships with intent-anchored tests (WHY in the docstring). ` +
  `(4) Run your targeted tests AND the cumulative regression list with: python -m pytest <files> -q -p no:cacheprovider --tb=short. Report REAL output. ` +
  `(5) ruff check on every file you touched; fix what it flags. ` +
  `(6) NEVER run git commit/push/checkout. NEVER touch the live memorymaster.db (3.2GB, in production) except where YOUR step explicitly requires reading it; tests use tmp DBs. ` +
  `(7) The sensitivity filter is sacred: any spool/replay path MUST route through svc.ingest so the filter applies — never raw INSERT into claims. ` +
  `(8) Flag-gated steps: the direct/legacy path stays intact as the else-branch; flag default OFF unless the spec says otherwise.`

const STEPS = [
  { n: 8,  hint: 'RO recall + access spool behind MEMORYMASTER_WAL_DISCIPLINE: _record_accesses RO branch, read_only store plumb, context_hook RO, recall hook template + test_ro_recall.py' },
  { n: 9,  hint: 'Ambient-write spool under flag for auto-ingest + dream-sync hook templates + dream_bridge.py, direct path as else-branch + test_ambient_spool.py (row parity)' },
  { n: 10, hint: 'init_db user_version fast-path behind MEMORYMASTER_INITDB_FASTPATH + test_initdb_fastpath.py (stamp mismatch forces full path). Do not benchmark the live DB; the operator re-measures after merge.' },
  { n: 11, hint: 'Observability: integrity/spool/busy-error metrics via record_event, dashboard panels, setup-hooks.py regeneration + test_integrity_metrics.py' },
  { n: 12, hint: 'Chaos soak harness: tests/soak/chaos_soak.py (pytest soak marker, 12 simulated writers vs VACUUM INTO fixture copy, kill rounds, quick_check gates) + scripts/run_chaos_soak.ps1. BUILD only — the gated run happens after merge.' },
]

const STEP_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['step', 'status', 'files_touched', 'tests_added', 'test_output_tail', 'spec_deviations', 'notes'],
  properties: {
    step: { type: 'integer' },
    status: { type: 'string', enum: ['done', 'failed', 'skipped_already_exists'] },
    files_touched: { type: 'array', items: { type: 'string' } },
    tests_added: { type: 'array', items: { type: 'string' } },
    test_output_tail: { type: 'string', description: 'the REAL last lines of your pytest run (targeted + cumulative)' },
    spec_deviations: { type: 'array', items: { type: 'string' }, description: 'every place reality differed from the spec and what you did' },
    notes: { type: 'string' },
  },
}

phase('Build')
const results = []
let cumulativeTests = ['tests/test_verbatim_store_pragmas.py','tests/test_open_conn.py','tests/test_integrity_job.py','tests/test_fk_repair.py','tests/test_qdrant_reconcile.py','tests/test_spool.py']
for (const s of STEPS) {
  const r = await agent(
    `${RULES}\n\nYOUR STEP: #${s.n} — ${s.hint}\nRead the full step text in the spec (section for step ${s.n}) — the hint above is a summary, the spec is authoritative.\n` +
    `CUMULATIVE REGRESSION LIST (run these after your own tests pass; earlier steps added them this run): ${cumulativeTests.length ? cumulativeTests.join(' ') : '(none yet — you are first)'}\n` +
    `Report honestly. status='failed' if your tests do not pass — do not paper over.`,
    { label: `step-${s.n}`, phase: 'Build', schema: STEP_SCHEMA })
  results.push(r)
  if (!r || r.status === 'failed') {
    log(`step ${s.n} ${r ? 'FAILED' : 'returned null'} — aborting chain for operator intervention`)
    break
  }
  cumulativeTests.push(...(r.tests_added || []))
  log(`step ${s.n}: ${r.status} (${(r.files_touched || []).length} files, ${(r.tests_added || []).length} test files)`)
}

return {
  completed: results.filter(r => r && r.status !== 'failed').length,
  total: STEPS.length,
  steps: results,
  cumulative_tests: cumulativeTests,
}
