export const meta = {
  name: 'mm4-surfaces',
  description: 'MM v4 P5: release-grade surfaces — docs consolidation + README 15-min quickstart + refreshed badges, MCP tool reference, governance dashboard panels (verify/complete the existing /api/conflicts /api/recall-analysis /api/triage routes), stranger-test.',
  whenToUse: 'Phase 5 of the v4 consolidation program — DX, docs, governance UI; the distributability prerequisite for the v4.0.0 release.',
  phases: [
    { title: 'Design', detail: 'survey docs, MCP tools, which dashboard governance routes already render vs are JSON-only' },
    { title: 'Build', detail: 'docs consolidation + README quickstart + badge refresh + MCP reference + complete governance panels' },
    { title: 'StrangerTest', detail: 'adversarial: follow ONLY the README to install->ingest->recall->dashboard in a clean venv; report friction' },
  ],
}

const REPO = 'G:/_OneDrive/OneDrive/Desktop/Py Apps/memorymaster'

const SAFETY =
  'Repo: ' + REPO + ', branch omni/p5-surfaces (verify with git branch --show-current; STOP if different). ' +
  'CONTEXT: this is the v4 program P5 (surfaces/DX), the prerequisite for the v4.0.0 release. The codebase is post-restructure (7 subpackages: core/stores/recall/govern/knowledge/surfaces/bridges) with old import paths shimmed. Current README badges are STALE (tests-2732 should be ~2871, MCP-tools-24 should be the real count, CLI-commands-86). docs/ has ~29 files including stale audits/experiments (AUDIT-2026-*, *-audit-2026-*, v315/v316/v318-experiments/, storage-parity-*, v320-backlog) that should be ARCHIVED (moved to docs/archive/), keeping the living docs (README, docs/INTEGRATING.md [P4], handbook.md, ROADMAP.md, troubleshooting.md, cli-cookbook.md, architecture.md, adr/). The dashboard (memorymaster/surfaces/dashboard.py) ALREADY HAS routes /api/conflicts, /api/recall-analysis, /api/triage, /api/audit, /api/provenance, /api/integrity, /api/observability — VERIFY which render as actual HTML panels vs JSON-only before building anything (verify-before-build: do NOT rebuild what exists). ' +
  'NON-NEGOTIABLE: (1) Do NOT touch the live memorymaster.db. (2) Do NOT weaken the sensitivity filter / intake policy. (3) Keep tree importable. (4) NEVER git commit/push. (5) Full suite green (~12min, real tail) + ruff clean before done. (6) Badge numbers must be REAL (count actual tests via pytest --co, MCP tools via grep @mcp.tool, CLI commands via the subparser list) — never fabricate. (7) Archived docs = git mv into docs/archive/, not deleted. (8) Be honest in the stranger test about real friction.';

phase('Design')
const DESIGN_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['readme_gaps', 'real_counts', 'docs_archive_list', 'dashboard_panel_status', 'mcp_reference_plan', 'notes'],
  properties: {
    readme_gaps: { type: 'array', items: { type: 'string' }, description: 'what the README lacks for a 15-min stranger: quickstart steps, positioning vs mem0/Letta/Zep, stale badges' },
    real_counts: { type: 'object', additionalProperties: false, required: ['tests', 'mcp_tools', 'cli_commands'],
      properties: { tests: { type: 'integer' }, mcp_tools: { type: 'integer' }, cli_commands: { type: 'integer' } },
      description: 'ACTUAL measured counts for the badges' },
    docs_archive_list: { type: 'array', items: { type: 'string' }, description: 'docs/ files to git mv into docs/archive/' },
    dashboard_panel_status: { type: 'array', items: { type: 'string' }, description: 'per governance route (conflicts/recall-analysis/triage/audit/provenance): renders-as-panel vs JSON-only vs missing' },
    mcp_reference_plan: { type: 'string', description: 'how to generate the MCP tool reference doc from code (grep @mcp.tool + docstrings)' },
    notes: { type: 'string' },
  },
}
const design = await agent(
  SAFETY + '\n\nROLE: survey for P5. Read README.md, docs/, memorymaster/surfaces/dashboard.py, memorymaster/surfaces/mcp_server.py, cli. Produce: real counts (tests via "python -m pytest tests/ --co -q | tail -1", MCP tools via grep @mcp.tool, CLI commands via the argparse subparser list); README gaps for a 15-min stranger; the stale-docs archive list; the per-route dashboard panel status (renders vs JSON-only); the MCP reference generation plan. Read-only this phase.',
  { label: 'design-surfaces', phase: 'Design', schema: DESIGN_SCHEMA })

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
    deliverables: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string' },
  },
}
const build = await agent(
  SAFETY + '\n\nROLE: build P5 surfaces per the design (counts: ' + JSON.stringify(design && design.real_counts) + '; archive: ' + JSON.stringify(design && design.docs_archive_list) + '; panels: ' + JSON.stringify(design && design.dashboard_panel_status) + '). DELIVERABLES: ' +
  '(1) README.md: add a concrete 15-MINUTE QUICKSTART (pip install, configure, ingest a claim via CLI, recall it, open the dashboard), tighten positioning vs mem0/Letta/Zep (governance = the differentiator), and REFRESH the stale badges to the real measured counts. ' +
  '(2) docs/ consolidation: git mv the stale audits/experiments into docs/archive/ (keep living docs); add a one-line docs/README.md index pointing to README/INTEGRATING/handbook/troubleshooting/cli-cookbook/ROADMAP. ' +
  '(3) docs/MCP-TOOLS.md: a reference of the MCP tools generated from mcp_server.py (name + one-line purpose from each tool docstring), grouped logically. ' +
  '(4) Governance dashboard panels: for any /api/conflicts /api/recall-analysis /api/triage route that is JSON-only (not rendered), add a minimal rendered panel to the dashboard HTML so an operator sees conflicts-triage + recall-analysis without curl. If they already render, just verify + note. Do NOT rebuild existing panels. ' +
  'Keep changes minimal + correct. Intent-anchored tests where you add panel routes/handlers. Report status=failed if suite not green.',
  { label: 'build-surfaces', phase: 'Build', schema: BUILD_SCHEMA })

if (!build || build.status !== 'done') return { design, build }

phase('StrangerTest')
const ST_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['verdict', 'minutes_estimate', 'quickstart_works', 'friction', 'badges_accurate', 'issues'],
  properties: {
    verdict: { type: 'string', enum: ['pass', 'fail'] },
    minutes_estimate: { type: 'number', description: 'realistic minutes for a stranger to go install->ingest->recall->dashboard following ONLY the README' },
    quickstart_works: { type: 'boolean', description: 'true only if you actually ran the README quickstart steps end-to-end in a clean venv and they worked' },
    friction: { type: 'array', items: { type: 'string' }, description: 'every place a real stranger would get stuck' },
    badges_accurate: { type: 'boolean', description: 'README badge counts match reality' },
    issues: { type: 'array', items: { type: 'string' } },
  },
}
const stranger = await agent(
  SAFETY + '\n\nROLE: the STRANGER TEST (P5 exit gate). Following ONLY README.md (no other knowledge), in a CLEAN venv (python -m venv in a temp dir, pip install -e the repo), actually execute the quickstart: install, configure, ingest a claim via the documented CLI, recall it, and confirm the dashboard boots. Time it realistically. Verify the badge counts match reality (re-measure). Report the REAL friction a newcomer hits — be harsh and honest; a 15-min target that actually takes 40 is a FAIL. Clean up the temp venv.',
  { label: 'stranger-test', phase: 'StrangerTest', schema: ST_SCHEMA })

return { design, build, stranger }
