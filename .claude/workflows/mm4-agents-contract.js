export const meta = {
  name: 'mm4-agents-contract',
  description: 'MM v4 P4: formalize the 3-beat multi-agent memory contract (fetch / recall / distilled-ingest) as a documented spec + reference impls per agent class + per-agent provenance dashboard panel + e2e round-trip proof.',
  whenToUse: 'Phase 4 of the v4 consolidation program — the multi-agent contract.',
  phases: [
    { title: 'Design', detail: 'map the 3-beat per agent class (Claude/Codex/Hermes/generic): what exists vs the gap' },
    { title: 'Build', detail: 'INTEGRATING.md spec + Codex/generic session-end reference + per-agent provenance panel + e2e round-trip test' },
    { title: 'Verify', detail: 'adversarial: a claim round-trips (ingest->recall) per locally-testable agent class; doc matches reality' },
  ],
}

const REPO = 'G:/_OneDrive/OneDrive/Desktop/Py Apps/memorymaster'

const SAFETY =
  'Repo: ' + REPO + ', branch omni/p4-agents (verify with git branch --show-current; STOP if different). ' +
  'CONTEXT: the "3-beat contract" is how any agent uses MemoryMaster: BEAT 1 session-start FETCH (inject recent claims/context), BEAT 2 on-demand RECALL (search DB per prompt), BEAT 3 session-end DISTILLED INGEST (<=3 distilled learnings, source_agent set). ' +
  'CURRENT STATE (verify against real code first): the hook templates in memorymaster/config_templates/hooks/ implement all 3 beats for CLAUDE (memorymaster-session-start.py = fetch, memorymaster-recall.py = recall, memorymaster-auto-ingest.py = distilled stop-ingest). memorymaster/surfaces/setup_hooks.py installs them + already has Codex AGENTS.md integration + CODEX_DIR awareness. The CODEX/GENERIC session-end automation is the GAP (AGENTS.md tells the agent to ingest, but there is no turnkey session-end script like Claude has). Hermes integration is partly EXTERNAL (a VM bridge built by another operator) — document the contract it must satisfy; do not claim to verify the external VM from here. ' +
  'NOTE: P1 chose the WAL-Discipline approach (NOT a write-broker), so all agents write through the same hardened connection envelope + (optionally) the intake policy + sensitivity filter at service.ingest — describe THAT as the shared writer discipline, not a broker. P3 made source_agent attribution reliable (intake policy), which is what makes a per-agent provenance view meaningful now. ' +
  'NON-NEGOTIABLE: (1) Do NOT weaken the sensitivity filter or intake policy. (2) Reference scripts must set source_agent and route ingest through the documented path (CLI ingest or MCP), never a raw INSERT. (3) Do NOT touch the live memorymaster.db. (4) Keep tree importable. (5) NEVER git commit/push. (6) Full suite green (real tail) + ruff clean before done. (7) Be HONEST about what is locally verifiable vs externally dependent (Hermes VM).';

phase('Design')
const DESIGN_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['beat_matrix', 'codex_gap', 'provenance_data_available', 'spec_path', 'notes'],
  properties: {
    beat_matrix: { type: 'array', items: { type: 'string' }, description: 'per agent class (claude/codex/hermes/generic) x 3 beats: what exists (file:line) vs missing' },
    codex_gap: { type: 'string', description: 'exactly what the Codex/generic session-end ingest needs — and what AGENTS.md already says' },
    provenance_data_available: { type: 'string', description: 'how source_agent is now reliably set (intake policy) + what the dashboard would query for a per-agent ingest/recall view' },
    spec_path: { type: 'string' },
    notes: { type: 'string' },
  },
}
const design = await agent(
  SAFETY + '\n\nROLE: map the 3-beat contract. Read the hook templates + setup_hooks + the dashboard. Produce: (1) a per-agent-class x 3-beat matrix grounded in real file:line; (2) the precise Codex/generic session-end gap; (3) what a per-agent provenance dashboard panel would show + the query. Write the integration spec to ' + REPO + '/.planning/P4-AGENTS-CONTRACT.md. Read-only this phase.',
  { label: 'design-contract', phase: 'Design', schema: DESIGN_SCHEMA })

phase('Build')
const BUILD_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['status', 'files_touched', 'tests_added', 'full_suite_tail', 'ruff_clean', 'deliverables', 'notes'],
  properties: {
    status: { type: 'string', enum: ['done', 'failed'] },
    files_touched: { type: 'array', items: { type: 'string' } },
    tests_added: { type: 'array', items: { type: 'string' } },
    full_suite_tail: { type: 'string' },
    ruff_clean: { type: 'boolean' },
    deliverables: { type: 'array', items: { type: 'string' }, description: 'which of: INTEGRATING doc, codex/generic session-end ref script, provenance dashboard panel, e2e round-trip test' },
    notes: { type: 'string' },
  },
}
const build = await agent(
  SAFETY + '\n\nROLE: implement P4 per .planning/P4-AGENTS-CONTRACT.md (design summary: ' + JSON.stringify(design && design.codex_gap) + '). DELIVERABLES: ' +
  '(1) docs/INTEGRATING.md — the 3-beat contract spec: the three beats, the shared WAL-Discipline writer discipline, the source_agent + <=3-distilled + no-session-state rules (the intake policy), and a per-agent-class how-to (Claude=installed hooks; Codex/generic=the reference script below; Hermes=the contract its bridge must satisfy). ' +
  '(2) A turnkey Codex/generic session-end ingest reference (a script under scripts/ or a config_template) that distills <=3 learnings and ingests them via the CLI/MCP with source_agent set — closing the gap; wire a mention into setup_hooks Codex path + AGENTS.md guidance. ' +
  '(3) A per-agent provenance dashboard panel (memorymaster/surfaces/dashboard*.py) showing ingest/recall counts by source_agent (now reliable post-intake-policy). ' +
  '(4) An e2e round-trip TEST (tests/test_agent_contract_roundtrip.py): for the locally-testable path(s), ingest a claim with a given source_agent via the documented path and assert recall/query returns it AND the provenance view counts it under that agent. ' +
  'Intent-anchored tests; report status=failed if suite not green.',
  { label: 'build-contract', phase: 'Build', schema: BUILD_SCHEMA })

if (!build || build.status !== 'done') return { design, build }

phase('Verify')
const VERIFY_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['verdict', 'roundtrip_proven', 'doc_matches_reality', 'honest_about_external', 'issues'],
  properties: {
    verdict: { type: 'string', enum: ['pass', 'fail'] },
    roundtrip_proven: { type: 'string', description: 'which agent classes were actually round-trip-proven locally vs documented-only' },
    doc_matches_reality: { type: 'boolean', description: 'INTEGRATING.md claims match the real hooks/scripts (spot-checked)' },
    honest_about_external: { type: 'boolean', description: 'Hermes/external dependencies are marked as such, not falsely claimed verified' },
    issues: { type: 'array', items: { type: 'string' } },
  },
}
const verify = await agent(
  SAFETY + '\n\nROLE: adversarial verifier. Builder deliverables: ' + JSON.stringify(build.deliverables) + '. ' +
  'AUDIT: (1) Run the e2e round-trip test; with a throwaway script, actually ingest a claim via the documented Codex/generic reference path and assert recall returns it under the right source_agent. (2) Spot-check INTEGRATING.md against the real hook files — any claim that does not match reality is an issue. (3) Confirm the doc is HONEST about Hermes/external being documented-not-verified (not falsely claimed e2e-proven). (4) Confirm the reference ingest script sets source_agent and uses the documented ingest path (not a raw INSERT bypassing the filter). Report pass/fail with specifics; delete throwaways.',
  { label: 'verify-contract', phase: 'Verify', schema: VERIFY_SCHEMA })

return { design, build, verify }
