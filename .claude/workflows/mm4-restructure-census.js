export const meta = {
  name: 'mm4-restructure-census',
  description: 'MM v4 P2-WF1: usage census of all 138 modules — import graph, activity, external surface — into a kill/keep/merge scorecard + subpackage map (read-only)',
  whenToUse: 'Phase 2 census stage of the MemoryMaster v4 consolidation program. Read-only; the operator rules on verdicts before any move.',
  phases: [
    { title: 'Census', detail: '4 analysts: import graph, git activity, external surface, subpackage clustering' },
    { title: 'Scorecard', detail: 'kill/keep/merge verdicts + subpackage map -> .planning/P2-CENSUS.md' },
  ],
}

const REPO = 'G:/_OneDrive/OneDrive/Desktop/Py Apps/memorymaster'

const COMMON =
  `Repo: ${REPO} (MemoryMaster v3.28+P1, branch omni/p2-restructure). ~138 Python modules sit FLAT in memorymaster/ (plus jobs/, migrations/, connectors/ subpackages). ` +
  `Target layout for P2: core/ (models, lifecycle, service, config, policy), stores/ (sqlite _storage_* splits, postgres, factory, migrations), recall/ (retrieval, fusion, tokenizer, query_cache, expansion, embeddings, qdrant, context_hook), ` +
  `govern/ (steward, llm_steward, jobs, probes, resolvers, dedupe, budget), knowledge/ (wiki_*, vault_*, entities, rules, rule_miner, closets), surfaces/ (cli*, mcp_server, dashboard*), bridges/ (dream, atlas, hermes/openclaw db_merge+delta_sync, connectors). ` +
  `RULES: READ-ONLY — no edits, no commits, no DB writes (open memorymaster.db only with mode=ro if needed). Ground every claim in real evidence (file reads, git commands, grep) — never guess. PYTHONIOENCODING=utf-8 for python subprocesses; ASCII-safe output.`

phase('Census')
const ANALYSTS = [
  {
    key: 'import-graph',
    schema: {
      type: 'object', additionalProperties: false,
      required: ['entry_points', 'unreachable_modules', 'high_fanin_top15', 'cycles', 'notes'],
      properties: {
        entry_points: { type: 'array', items: { type: 'string' }, description: 'the real entry surfaces found (console scripts, hooks, scheduled tasks)' },
        unreachable_modules: { type: 'array', items: { type: 'string' }, description: 'modules NOT importable from any entry point (orphan candidates)' },
        high_fanin_top15: { type: 'array', items: { type: 'string' }, description: '"module: N importers" — the load-bearing core' },
        cycles: { type: 'array', items: { type: 'string' }, description: 'import cycles found, as A -> B -> A chains' },
        notes: { type: 'string' },
      },
    },
    prompt:
      `${COMMON}\n\nTASK: build the real import graph. Write a throwaway python script (in temp, not the repo) that walks memorymaster/**/*.py with ast.parse, extracts internal imports (memorymaster.*), and computes: ` +
      `reachability from the true entry points (pyproject console scripts: cli, mcp_server, dashboard, llm_steward; PLUS the hook entrypoints context_hook/dream_bridge/setup_hooks and the steward-cycle hook template; PLUS service.py as the library API root), ` +
      `fan-in per module, and import cycles. Modules unreachable from ALL of those are orphan candidates — list them exactly. Also report the top-15 fan-in modules (the load-bearing set).`,
  },
  {
    key: 'activity',
    model: 'haiku',
    schema: {
      type: 'object', additionalProperties: false,
      required: ['dormant_6mo', 'active_30d', 'single_commit_modules', 'loc_map_top20', 'notes'],
      properties: {
        dormant_6mo: { type: 'array', items: { type: 'string' }, description: 'modules with no commit touching them in 6+ months, as "module: last-touch date"' },
        active_30d: { type: 'array', items: { type: 'string' } },
        single_commit_modules: { type: 'array', items: { type: 'string' }, description: 'modules created in one commit and never touched again (scaffold smell)' },
        loc_map_top20: { type: 'array', items: { type: 'string' } },
        notes: { type: 'string' },
      },
    },
    prompt:
      `${COMMON}\n\nTASK: git-activity census. For every memorymaster/**/*.py: last commit date touching it (git log -1 --format=%cs -- path), total commits touching it, and LOC. ` +
      `Report: dormant (6+ months untouched), active (touched in last 30 days), single-commit scaffolds (created once, never edited — strongest dead-code smell when combined with low fan-in), top-20 LOC.`,
  },
  {
    key: 'external-surface',
    schema: {
      type: 'object', additionalProperties: false,
      required: ['cli_command_modules', 'mcp_tool_modules', 'hook_referenced', 'script_referenced', 'doc_only_modules', 'notes'],
      properties: {
        cli_command_modules: { type: 'array', items: { type: 'string' }, description: 'modules backing CLI subcommands (module: commands)' },
        mcp_tool_modules: { type: 'array', items: { type: 'string' } },
        hook_referenced: { type: 'array', items: { type: 'string' }, description: 'modules imported/invoked by installed hooks or config_templates' },
        script_referenced: { type: 'array', items: { type: 'string' }, description: 'modules invoked by scripts/ or scheduled tasks' },
        doc_only_modules: { type: 'array', items: { type: 'string' }, description: 'modules mentioned only in docs/ or nowhere at all' },
        notes: { type: 'string' },
      },
    },
    prompt:
      `${COMMON}\n\nTASK: external-surface census — what reaches each module from OUTSIDE python imports. Map: CLI subcommands -> handler modules (cli.py + cli_handlers_*), @mcp.tool implementations -> modules, config_templates/hooks/* + installed-hook patterns -> modules, scripts/*.py|ps1|sh -> modules, .claude/skills + workflows -> modules. ` +
      `Modules with NO external surface AND low import fan-in are kill/merge candidates; modules with hook/script surface must NEVER be killed silently. Be exhaustive on the hook templates — they run in production.`,
  },
  {
    key: 'clustering',
    schema: {
      type: 'object', additionalProperties: false,
      required: ['subpackage_map', 'awkward_modules', 'split_candidates', 'notes'],
      properties: {
        subpackage_map: { type: 'object', additionalProperties: { type: 'array', items: { type: 'string' } }, description: 'target subpackage -> ordered module list (every module assigned exactly once)' },
        awkward_modules: { type: 'array', items: { type: 'string' }, description: 'modules that resist the taxonomy + why (one line each)' },
        split_candidates: { type: 'array', items: { type: 'string' }, description: 'the 13 over-800-LOC modules with a one-line split proposal each' },
        notes: { type: 'string' },
      },
    },
    prompt:
      `${COMMON}\n\nTASK: assign EVERY module under memorymaster/ (including jobs/, connectors/; migrations stays as-is under stores/) to exactly one target subpackage. Read module docstrings + key imports to decide — do not guess from filenames alone. ` +
      `Flag modules that resist the taxonomy (e.g., used by both recall and govern). For the 13 known over-800-LOC modules (postgres_store 2613, context_hook 2161, service 1819, steward 1739, mcp_server 1586, dashboard 1568, cli_handlers_basic 1566, operator 1453, llm_steward 1076, cli_handlers_curation 1062, _storage_sources 1006, wiki_engine 962, _storage_schema 867) propose a one-line split. Keep migration risk in view: the map should minimize cross-subpackage cycles.`,
  },
]

async function batched(items, makeThunk, size) {
  const out = []
  for (let i = 0; i < items.length; i += size) {
    const res = await parallel(items.slice(i, i + size).map((it) => () => makeThunk(it)))
    out.push(...res)
  }
  return out
}
const results = await batched(ANALYSTS, (a) =>
  agent(a.prompt, { label: a.key, phase: 'Census', schema: a.schema, ...(a.model ? { model: a.model } : {}) }), 4)
const byKey = {}
results.forEach((r, i) => { byKey[ANALYSTS[i].key] = r || null })
log(`census: ${results.filter(Boolean).length}/4 analysts returned`)

phase('Scorecard')
const SCORE_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['scorecard_path', 'kill_candidates', 'merge_candidates', 'keep_count', 'subpackage_map_final', 'headline'],
  properties: {
    scorecard_path: { type: 'string' },
    kill_candidates: { type: 'array', items: { type: 'string' }, description: '"module: evidence summary" — ONLY modules with zero import reach AND zero external surface AND dormant' },
    merge_candidates: { type: 'array', items: { type: 'string' }, description: '"module -> target: rationale"' },
    keep_count: { type: 'integer' },
    subpackage_map_final: { type: 'object', additionalProperties: { type: 'array', items: { type: 'string' } } },
    headline: { type: 'string' },
  },
}
const score = await agent(
  `${COMMON}\n\nTASK: compile the P2 census scorecard from the four analysts' structured results:\n${JSON.stringify(byKey)}\n\n` +
  `Write ${REPO}/.planning/P2-CENSUS.md: one table row per module (subpackage assignment, fan-in, last-touch, LOC, external surface, verdict kill/merge/keep with one-line evidence), the final subpackage map, the over-800 split plans, and a migration-order proposal (least-coupled subpackage first). ` +
  `VERDICT RULES (conservative, operator-mandated): kill ONLY when a module has zero import reachability AND zero external surface (no CLI/MCP/hook/script/skill reference) AND is dormant — any single piece of usage evidence means keep or merge. Prefer merge-into-sibling over kill. Cross-check each kill candidate against ALL FOUR analysts before listing it. Return the structured summary.`,
  { label: 'scorecard', phase: 'Scorecard', schema: SCORE_SCHEMA })

return { census: byKey, scorecard: score }
