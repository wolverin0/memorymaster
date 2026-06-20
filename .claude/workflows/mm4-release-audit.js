export const meta = {
  name: 'mm4-release-audit',
  description: 'MM v4 P6: release-gate audit before tagging v4.0.0 — parallel skeptics on secrets, packaging/install/entry-points, import+shim correctness, breaking-change/migration completeness -> SHIP/HOLD verdict.',
  whenToUse: 'Phase 6 of the v4 program — the final safety gate before the v4.0.0 PyPI release.',
  phases: [
    { title: 'Audit', detail: '4 parallel release-risk skeptics' },
    { title: 'Verdict', detail: 'synthesize SHIP/HOLD with any crit/high blockers' },
  ],
}

const REPO = 'G:/_OneDrive/OneDrive/Desktop/Py Apps/memorymaster'

const COMMON =
  'Repo: ' + REPO + ', branch omni/p6-release (verify with git branch --show-current). This is the FINAL release gate before tagging v4.0.0 and publishing to PyPI. The codebase completed a 5-phase v4 consolidation (reliability, 138->7 subpackage restructure with shims, ingest governance + sensitivity hardening, multi-agent contract, surfaces). Full suite is green (2873 passed), CI green on main. ' +
  'RULES: READ-ONLY — do NOT modify code, do NOT touch the live memorymaster.db, do NOT commit/push. Report findings with severity (crit/high/med/low). A release-BLOCKER is crit or high ONLY. Ground every finding in evidence (file:line, a command you ran). Be proportionate — this is a release gate, not a nitpick hunt; do not invent blockers. PYTHONIOENCODING=utf-8 for python subprocesses.';

phase('Audit')
const FINDINGS_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['dimension', 'blockers', 'non_blocking', 'evidence', 'verdict'],
  properties: {
    dimension: { type: 'string' },
    blockers: { type: 'array', items: { type: 'string' }, description: 'crit/high issues that should HOLD the release (empty if none)' },
    non_blocking: { type: 'array', items: { type: 'string' }, description: 'med/low issues to note for post-release' },
    evidence: { type: 'array', items: { type: 'string' }, description: 'commands run + results that ground the verdict' },
    verdict: { type: 'string', enum: ['ship', 'hold'] },
  },
}
const SKEPTICS = [
  { key: 'secrets', prompt:
    'AUDIT DIMENSION: secrets in the shipped artifact. Build the sdist+wheel (python -m build OR pip wheel . into a temp dir; if build not installed, inspect what setuptools would package via the MANIFEST/package-data + git ls-files). Grep the would-be-shipped files for real credentials/tokens/keys/private IPs/personal absolute paths (NOT test fixtures with obviously-fake values). Confirm memorymaster.db / *.db / .env are NOT in the package. Confirm the sensitivity filter module ships. A real secret or the live DB in the package = CRIT blocker.' },
  { key: 'packaging', prompt:
    'AUDIT DIMENSION: packaging + install + entry points. In a CLEAN temp venv, pip install the repo (pip install . or -e .[mcp]); confirm it installs without error; run each of the 5 console entry points minimally (memorymaster --help, memorymaster-mcp import, memorymaster-dashboard import, memorymaster-steward import, memorymaster-setup import / --help); confirm "import memorymaster" + each subpackage (core/stores/recall/govern/knowledge/surfaces/bridges) imports clean. pyproject metadata sane (version, deps, classifiers, license). A broken install or a dead entry point = HIGH blocker. Clean up the venv.' },
  { key: 'shims', prompt:
    'AUDIT DIMENSION: backward-compat import shims (the restructure moved 138 modules; old paths must still resolve for one minor version). Write a throwaway script that imports a representative sample of OLD top-level paths (memorymaster.service, memorymaster.storage, memorymaster.context_hook, memorymaster.wiki_engine, memorymaster.steward, memorymaster.mcp_server, memorymaster.llm_budget, memorymaster.models, memorymaster.security) and asserts each resolves (via sys.modules alias) to the NEW subpackage module object. Any old path that ImportErrors = HIGH blocker (silent breakage for existing importers). Also confirm the installed production hooks reference paths that still resolve.' },
  { key: 'migration', prompt:
    'AUDIT DIMENSION: breaking-change + migration completeness for a 3.28 -> 4.0 MAJOR bump. Verify the CHANGELOG/README document the new subpackage import paths + the deprecation of old paths. Check there is NO un-migrated DB schema break (migrations 0001-0007 present; a 3.28 DB opens cleanly under 4.0 — test by pointing init_db at a fresh tmp DB and confirming it builds). Confirm nothing in the public CLI/MCP surface was REMOVED without a shim (compare entry points + @mcp.tool list to 3.28). A silent removed-public-API or a schema break with no migration = HIGH blocker.' },
]
async function batched(items, make, size) {
  const out = []
  for (let i = 0; i < items.length; i += size) out.push(...await parallel(items.slice(i, i + size).map((it) => () => make(it))))
  return out
}
const findings = await batched(SKEPTICS, (s) =>
  agent(COMMON + '\n\n' + s.prompt, { label: 'audit:' + s.key, phase: 'Audit', schema: { ...FINDINGS_SCHEMA } }), 4)
const valid = findings.filter(Boolean)
log('audit: ' + valid.length + '/4 skeptics returned; holds=' + valid.filter(f => f.verdict === 'hold').length)

phase('Verdict')
const VERDICT_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['ship', 'blockers', 'non_blocking_summary', 'rationale'],
  properties: {
    ship: { type: 'boolean', description: 'true = clear to tag v4.0.0 and publish; false = HOLD' },
    blockers: { type: 'array', items: { type: 'string' }, description: 'the crit/high issues that must be fixed before release (empty if ship)' },
    non_blocking_summary: { type: 'array', items: { type: 'string' }, description: 'med/low to carry as post-release follow-ups' },
    rationale: { type: 'string' },
  },
}
const verdict = await agent(
  COMMON + '\n\nROLE: synthesize the release verdict from the 4 skeptics:\n' + JSON.stringify(valid) + '\n\n' +
  'SHIP only if there are ZERO crit/high blockers across all dimensions. Independently sanity-check any claimed blocker before accepting it (do not HOLD on a misdiagnosis; do not SHIP past a real one). Return ship=true/false, the deduped blocker list, the non-blocking follow-ups, and your rationale.',
  { label: 'release-verdict', phase: 'Verdict', schema: VERDICT_SCHEMA })

return { findings: valid, verdict }
