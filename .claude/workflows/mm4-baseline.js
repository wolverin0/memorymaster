export const meta = {
  name: 'mm4-baseline',
  description: 'MM v4 program P0: fix self-knowledge (graphify hook, stale context docs) and freeze the BASELINE metrics scorecard the whole consolidation program is judged against',
  whenToUse: 'Phase 0 of the MemoryMaster v4 consolidation program. Run once at program start; re-run only to refresh the baseline.',
  phases: [
    { title: 'Repair+Measure', detail: 'fix graphify hook encoding, regenerate context docs, fan-out metric probes (read-only on the DB)' },
    { title: 'Synthesize', detail: 'compile BASELINE.html scorecard + headline numbers' },
  ],
}

const REPO = 'G:/_OneDrive/OneDrive/Desktop/Py Apps/memorymaster'
const DB = `${REPO}/memorymaster.db`
const DATE = '2026-06-09'
const BATCH = 4

async function batched(items, makeThunk, size) {
  const out = []
  for (let i = 0; i < items.length; i += size) {
    const res = await parallel(items.slice(i, i + size).map((it, j) => () => makeThunk(it, i + j)))
    out.push(...res)
    log(`  batch ${Math.floor(i / size) + 1}/${Math.ceil(items.length / size)} done`)
  }
  return out
}

const COMMON =
  `Repo: ${REPO} (MemoryMaster, Python 3.10+, SQLite). The live DB is ${DB} (~3GB, in production use by other agents RIGHT NOW). ` +
  `HARD RULES: open the DB READ-ONLY (sqlite3 'file:...?mode=ro', uri=True) for every query — you must NOT write to it. ` +
  `Do NOT git commit anything. Do NOT push. Report REAL measured output, never estimates. ` +
  `Windows console is cp1252 — use PYTHONIOENCODING=utf-8 for python subprocesses and keep your own printed output ASCII-safe.`

// ---------- Phase 1: repair + docs + measure (independent, batched) ----------
phase('Repair+Measure')

const TASKS = [
  {
    key: 'fix-graphify-hook',
    schema: {
      type: 'object', additionalProperties: false,
      required: ['hook_path', 'fix_applied', 'verified_clean', 'graph_report_regenerated', 'notes'],
      properties: {
        hook_path: { type: 'string' },
        fix_applied: { type: 'string', description: 'exact change made' },
        verified_clean: { type: 'boolean', description: 'true only if you RAN the hook/watcher and saw no charmap error' },
        graph_report_regenerated: { type: 'boolean', description: 'true only if GRAPH_REPORT.md is now non-empty and current' },
        notes: { type: 'string' },
      },
    },
    prompt:
      `${COMMON}\n\nTASK: fix the graphify post-commit hook that crashes on EVERY commit with ` +
      `"'charmap' codec can't encode character '\\u2192'" — a Windows cp1252 stdout failure that has left ${REPO}/graphify-out/GRAPH_REPORT.md at 0 bytes.\n` +
      `1. LOCATE the hook chain: inspect ${REPO}/.git/hooks/post-commit (and post-checkout), follow what script(s) it invokes (likely a graphify watcher/rebuild script — check ~/.claude/skills/graphify/ and wherever the hook points). Quote the failing print site.\n` +
      `2. FIX the encoding at the most durable point: prefer sys.stdout/sys.stderr .reconfigure(encoding='utf-8', errors='replace') at the top of the crashing Python script, or set PYTHONUTF8=1/PYTHONIOENCODING=utf-8 in the hook's invocation env. Minimal diff. If the crashing file is OUTSIDE the repo (e.g. a global skill script), fix it there and say so.\n` +
      `3. VERIFY by RUNNING the rebuild script directly (same way the hook calls it) — do NOT create a git commit to test. Confirm zero charmap errors in its output.\n` +
      `4. REGENERATE the graph artifacts so ${REPO}/graphify-out/GRAPH_REPORT.md is non-empty and reflects the current tree (run the rebuild/regeneration path the hook uses).\n` +
      `Report honestly — verified_clean=true ONLY with real run output.`,
  },
  {
    key: 'regen-docs-arch',
    schema: {
      type: 'object', additionalProperties: false,
      required: ['files_written', 'notes'],
      properties: { files_written: { type: 'array', items: { type: 'string' } }, notes: { type: 'string' } },
    },
    prompt:
      `${COMMON}\n\nTASK: regenerate ${REPO}/.planning/codebase/ARCHITECTURE.md and STRUCTURE.md from THE CURRENT TREE (the existing files describe v2.0.0 from a Linux mount — badly stale; current is v3.26.1+, ~110 modules).\n` +
      `Read the real code (memorymaster/ package layout, service.py, jobs/, migrations/, the wiki/vault/rule/recall modules, mcp_server tools). Write accurate, concise docs (60-90 lines each): layered architecture diagram (entry surfaces -> MemoryService -> stores/jobs -> optional backends), the claim lifecycle, the steward pipeline incl batch_limit, the recall fusion stack (lexical/vector/entity/graph/RRF auto-gate), and a truthful directory tree with one-line purposes. Use Windows path ${REPO} as the root in STRUCTURE.md. Overwrite the two files.`,
  },
  {
    key: 'regen-docs-quality',
    schema: {
      type: 'object', additionalProperties: false,
      required: ['files_written', 'notes'],
      properties: { files_written: { type: 'array', items: { type: 'string' } }, notes: { type: 'string' } },
    },
    prompt:
      `${COMMON}\n\nTASK: regenerate ${REPO}/.planning/codebase/CONCERNS.md, TESTING.md, CONVENTIONS.md, STACK.md, INTEGRATIONS.md from THE CURRENT TREE (existing versions describe v2.0.0 — stale).\n` +
      `Ground every statement in the real code/config: pyproject.toml extras + version, pytest.ini + actual test-file count (count tests/test_*.py), the migrations framework (memorymaster/migrations/0001-0007), .claude/rules/* conventions, MCP/Qdrant/Gemini/claude_cli/Postgres integration touchpoints. CONCERNS.md must reflect TODAY's risks (12 concurrent per-pane mcp_server writers on one SQLite file — corruption happened 2026-06-05; intake-vs-steward throughput; Qdrant fire-and-forget sync with no reconciliation; ~110 flat modules sprawl; per-pane MCP servers), and explicitly note which v2.0-era concerns are FIXED (migrations framework shipped v3.20; postgres parity fixed v3.27 batch 1; busy_timeout/WAL hardening v3.27 batch 2). Overwrite the five files.`,
  },
  {
    key: 'perf-recall',
    schema: {
      type: 'object', additionalProperties: false,
      required: ['recall_p50_ms', 'recall_p95_ms', 'samples', 'init_db_warm_s', 'methodology', 'notes'],
      properties: {
        recall_p50_ms: { type: 'number' }, recall_p95_ms: { type: 'number' }, samples: { type: 'integer' },
        init_db_warm_s: { type: 'number' }, methodology: { type: 'string' }, notes: { type: 'string' },
      },
    },
    prompt:
      `${COMMON}\n\nTASK: measure recall latency and init cost against the LIVE 3GB DB — these fire on EVERY user prompt, so they are the program's most user-facing numbers.\n` +
      `1. RECALL p50/p95: write a python script that calls the recall path the UserPromptSubmit hook uses (memorymaster.context_hook — find its public entry, e.g. the function the hook script invokes; read ~/.claude/hooks/memorymaster-recall.py to see the exact call) with >=20 realistic varied queries (mix of project topics: 'steward validation', 'postgres parity', 'recall fusion', etc.). Time each end-to-end in-process (time.perf_counter around the call), report p50/p95 in ms and the sample count. Use the live DB read-only-safely (the recall path itself opens connections — that's fine, it's a read path; just don't ingest).\n` +
      `2. INIT: time MemoryService(db_target=...).init_db() on the LIVE db (warm, second call) — this is the MCP-server boot cost. One number in seconds.\n` +
      `3. Note any timeouts/errors honestly. Describe methodology so the FINAL re-measurement can replicate it exactly.`,
  },
  {
    key: 'db-stats',
    model: 'haiku',
    schema: {
      type: 'object', additionalProperties: false,
      required: ['db_size_gb', 'claims_by_status', 'candidates_inflow_per_day_7d', 'inflow_by_source_agent_7d', 'conflicted', 'verbatim_rows', 'events_rows', 'steward_validated_per_day_7d', 'notes'],
      properties: {
        db_size_gb: { type: 'number' },
        claims_by_status: { type: 'object', additionalProperties: { type: 'integer' } },
        candidates_inflow_per_day_7d: { type: 'number' },
        inflow_by_source_agent_7d: { type: 'object', additionalProperties: { type: 'integer' } },
        conflicted: { type: 'integer' }, verbatim_rows: { type: 'integer' }, events_rows: { type: 'integer' },
        steward_validated_per_day_7d: { type: 'number' }, notes: { type: 'string' },
      },
    },
    prompt:
      `${COMMON}\n\nTASK: capture the DB governance baseline via READ-ONLY SQL on ${DB}:\n` +
      `file size in GB; claims count by status; NEW claims (created_at) per day averaged over the last 7 days (inflow rate); that inflow split by source_agent; conflicted count; verbatim_memories count; events count; claims validated per day (last_validated_at) averaged over 7 days (steward throughput). Return exact numbers.`,
  },
  {
    key: 'code-census',
    model: 'haiku',
    schema: {
      type: 'object', additionalProperties: false,
      required: ['module_count', 'total_loc', 'over_800_loc', 'test_files', 'test_count', 'mcp_tool_count', 'top10_largest', 'notes'],
      properties: {
        module_count: { type: 'integer' }, total_loc: { type: 'integer' },
        over_800_loc: { type: 'array', items: { type: 'string' } },
        test_files: { type: 'integer' }, test_count: { type: 'integer' }, mcp_tool_count: { type: 'integer' },
        top10_largest: { type: 'array', items: { type: 'string' } }, notes: { type: 'string' },
      },
    },
    prompt:
      `${COMMON}\n\nTASK: code census for the consolidation baseline:\n` +
      `count .py modules under memorymaster/ (excluding __pycache__); total LOC; list every module >800 LOC (the project's own limit) as 'name: LOC'; count tests/test_*.py files; get collected test count via 'python -m pytest tests/ --co -q | tail -1'; count MCP tools (grep '@mcp.tool' or the registration pattern in mcp_server.py); list top-10 largest modules as 'name: LOC'.`,
  },
  {
    key: 'mcp-usage',
    model: 'haiku',
    schema: {
      type: 'object', additionalProperties: false,
      required: ['usage_available', 'tool_usage_counts', 'unused_tools', 'notes'],
      properties: {
        usage_available: { type: 'boolean' },
        tool_usage_counts: { type: 'object', additionalProperties: { type: 'integer' } },
        unused_tools: { type: 'array', items: { type: 'string' } },
        notes: { type: 'string' },
      },
    },
    prompt:
      `${COMMON}\n\nTASK: MCP tool usage mining (feeds the P5 curation decisions).\n` +
      `memorymaster has an mcp_usage tracking table (see memorymaster/mcp_usage.py for its schema + which sqlite file it writes — it may be a SEPARATE db file, check). If usage data exists, return per-tool call counts and the list of tools registered in mcp_server.py that have ZERO recorded calls. If the table/file doesn't exist or is empty, say usage_available=false honestly and list the registered tools in notes.`,
  },
]

const results = await batched(TASKS, (t) =>
  agent(t.prompt, { label: t.key, phase: 'Repair+Measure', schema: t.schema, ...(t.model ? { model: t.model } : {}) }),
  BATCH)

const byKey = {}
results.forEach((r, i) => { byKey[TASKS[i].key] = r || null })
log(`Repair+Measure: ${results.filter(Boolean).length}/${TASKS.length} tasks returned`)

// ---------- Phase 2: synthesize the BASELINE scorecard ----------
phase('Synthesize')
const SYNTH_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['baseline_path', 'headline', 'gaps_vs_north_star', 'notes'],
  properties: {
    baseline_path: { type: 'string' },
    headline: { type: 'string', description: '4-6 sentence executive summary of where the project stands, numbers included' },
    gaps_vs_north_star: { type: 'array', items: { type: 'string' }, description: 'one line per north-star property: current number vs target' },
    notes: { type: 'string' },
  },
}
const synth = await agent(
  `${COMMON}\n\nTASK: compile the MM v4 program BASELINE scorecard.\n` +
  `Here are the raw structured results from the measurement agents (JSON):\n${JSON.stringify(byKey)}\n\n` +
  `Write a single self-contained HTML file to ${REPO}/.planning/BASELINE-${DATE}.html: dark theme, a stat grid of the headline numbers, then one section per north-star property (durable/governed/precise/fast/multi-agent-safe/observable/distributable) showing measured-today vs target (targets: single writer + quick_check scheduled; backlog <=1 day inflow; published LongMemEval >= baseline; recall p50<300ms p95<1s, init<2s; 3 agent classes e2e; dashboards + fresh self-docs; v4.0.0 on PyPI). Include the repair outcomes (graphify hook fixed? docs regenerated?) and the full methodology notes so the P6 FINAL re-measurement replicates it. Mark LongMemEval as DEFERRED-to-P3 (7h wall-time, not run at baseline). ASCII-safe content. ` +
  `Then return: the file path, a headline summary, and one gap line per property.`,
  { label: 'synthesize-baseline', phase: 'Synthesize', schema: SYNTH_SCHEMA }
)

return {
  repair: { graphify_hook: byKey['fix-graphify-hook'], docs_arch: byKey['regen-docs-arch'], docs_quality: byKey['regen-docs-quality'] },
  metrics: { perf: byKey['perf-recall'], db: byKey['db-stats'], census: byKey['code-census'], mcp_usage: byKey['mcp-usage'] },
  baseline: synth,
}
