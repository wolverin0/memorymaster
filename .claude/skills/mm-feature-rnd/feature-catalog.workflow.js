export const meta = {
  name: 'mm-feature-rnd-catalog',
  description: 'Mine MemoryMaster for net-new feature candidates, generate full design specs, judge-panel score, synthesize a ranked feature catalog',
  phases: [
    { title: 'Scout', detail: 'mine roadmap / gated features / ADRs / novel ideas' },
    { title: 'Design', detail: 'full design spec per candidate' },
    { title: 'Judge', detail: 'score each design on value/effort/risk/differentiation' },
    { title: 'Synthesize', detail: 'ranked catalog' },
  ],
}

const REPO = 'G:/_OneDrive/OneDrive/Desktop/Py Apps/memorymaster'
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

const CONTEXT =
  `MemoryMaster is a production memory-reliability system for AI coding agents (Python, SQLite+FTS5 default / optional Postgres / optional Qdrant, FastMCP server, Obsidian wiki). ` +
  `Core primitives already shipped: 6-state claim lifecycle (candidate/confirmed/stale/superseded/conflicted/archived), event log + citations + idempotency, hybrid retrieval (lexical+vector+freshness+confidence+tier), bitemporal fields (event_time/valid_from/valid_until), entity registry + graph BFS, LLM steward governance + proposals, RL feedback + quality scoring, query classification (7 types), rule-shaped claims (claim_type='rule', trigger/action/rationale) + a rule_miner, wiki engine (compiled-truth + timeline), cross-project federation, RBAC + per-agent visibility, dashboard with SSE, Postgres parity, Docker/Helm. ` +
  `Competitors in the AI-memory space: mem0, Letta (MemGPT), Zep, MemPalace, ReflexioAI/claude-smart. MM's differentiators are lifecycle governance, bitemporal truth, the wiki read-layer, and rule-shaped claims.`

const SCOUT_SLICES = [
  { key: 'roadmap', prompt:
    `Read ROADMAP.md, docs/ROADMAP.md, docs/v320-backlog.md, docs/v316-roadmap.md. Extract NET-NEW features that are PLANNED-BUT-NOT-YET-BUILT (verify against the code — many backlog items have since shipped; e.g. migrations S1, rule-claims R1 are DONE). The conflict-resolution dashboard UI (D1) and Kuzu graph-retrieval are likely candidates. For each, note name, what it does, where it would live, and current status.` },
  { key: 'gated', prompt:
    `Find SCAFFOLDED / EXPERIMENTAL / ENV-GATED features that exist but are not promoted/complete. Read memorymaster/graph_store.py (Kuzu, MEMORYMASTER_RECALL_GRAPH), memorymaster/query_expansion.py (MEMORYMASTER_RECALL_QUERY_EXPANSION), memorymaster/qdrant_recall_fallback.py, memorymaster/rule_miner.py, and grep for MEMORYMASTER_* flags in memorymaster/config.py. For each half-built feature, propose what "completing/promoting it to a first-class feature" would entail.` },
  { key: 'improve', prompt:
    `Read .planning/codebase/CONCERNS.md and docs/IMPROVEMENT_PLAN.md. Surface IMPROVEMENTS TO EXISTING FEATURES that would be net-positive capability (e.g. better recall fusion, smarter dedup, richer wiki). STRICT: features/improvements ONLY — do NOT list bug fixes, test coverage, refactors, or cleanup (those are out of scope).` },
  { key: 'adr', prompt:
    `Read docs/architecture.md and skim docs/adr/*.md (14 ADRs). Identify architectural directions or primitives that are under-exploited and could power a NEW user-facing feature (e.g. the event log enables time-travel/audit views; bitemporal enables "what did I believe on date X"; entity graph enables relationship maps; quality scores enable a memory-health view).` },
  { key: 'novel', prompt:
    `Read README.md for positioning. Then IDEATE net-new features that would DIFFERENTIATE MemoryMaster vs mem0/Letta/Zep/claude-smart, grounded in MM's ACTUAL primitives (claims, lifecycle, bitemporal, wiki, entities, rules, steward, federation). Be concrete and buildable on the existing stack — no vaporware. Propose 4-6 distinct ideas.` },
]

const CANDIDATE_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['candidates'],
  properties: { candidates: { type: 'array', items: {
    type: 'object', additionalProperties: false,
    required: ['name', 'one_liner', 'where', 'status', 'why_valuable'],
    properties: {
      name: { type: 'string' },
      one_liner: { type: 'string' },
      where: { type: 'string', description: 'modules/surface it touches' },
      status: { type: 'string', enum: ['planned', 'scaffolded', 'improvement', 'novel'] },
      why_valuable: { type: 'string' },
    } } } },
}

const DESIGN_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['name', 'category', 'problem', 'design', 'surface', 'effort', 'risk', 'differentiation', 'test_plan', 'dependencies'],
  properties: {
    name: { type: 'string' },
    category: { type: 'string', enum: ['retrieval', 'governance', 'ui-dashboard', 'extraction', 'graph', 'integration', 'observability', 'other'] },
    problem: { type: 'string', description: 'the user problem / gap it solves' },
    design: { type: 'string', description: 'how it works, concretely, on MM\'s existing stack' },
    surface: { type: 'string', description: 'CLI / MCP tool / schema / dashboard / config changes it needs' },
    effort: { type: 'string', enum: ['S', 'M', 'L', 'XL'] },
    risk: { type: 'string', description: 'main risks / unknowns' },
    differentiation: { type: 'string', description: 'why this beats what mem0/Letta/Zep/claude-smart offer' },
    test_plan: { type: 'string' },
    dependencies: { type: 'string', description: 'what must exist first; "none" if standalone' },
  },
}

const JUDGE_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['value', 'effort_cost', 'risk', 'differentiation', 'total', 'verdict', 'rationale'],
  properties: {
    value: { type: 'integer', description: 'user value 1-5 (5 best)' },
    effort_cost: { type: 'integer', description: 'effort/cost 1-5 (5 = cheapest/smallest)' },
    risk: { type: 'integer', description: 'safety 1-5 (5 = lowest risk)' },
    differentiation: { type: 'integer', description: 'differentiation 1-5 (5 = unique)' },
    total: { type: 'integer', description: 'sum of the four (4-20)' },
    verdict: { type: 'string', enum: ['build-now', 'strong', 'maybe', 'skip'] },
    rationale: { type: 'string' },
  },
}

// ---- Phase 1: SCOUT ----
phase('Scout')
const scoutBatches = await batched(SCOUT_SLICES, (s) =>
  agent(
    `You are scouting NET-NEW feature candidates for MemoryMaster at ${REPO}.\n\n${CONTEXT}\n\n` +
    `TASK: ${s.prompt}\n\n` +
    `Read the named files, verify status against the actual code, and return candidate features. ` +
    `STRICT SCOPE: net-new features or improvements to existing features ONLY — NO bug fixes, test coverage, refactors, or cleanup. Read-only.`,
    { label: `scout:${s.key}`, phase: 'Scout', schema: CANDIDATE_SCHEMA, agentType: 'Explore' }
  ), BATCH)
const rawCandidates = scoutBatches.filter(Boolean).flatMap(b => b.candidates || [])
log(`Scout: ${rawCandidates.length} raw candidates`)

// Dedup/curate to a slate (one curator agent; candidates are short).
const curated = await agent(
  `Curate this list of MemoryMaster feature candidates into a DEDUPED slate of the strongest distinct features (merge near-duplicates, drop anything that is a bug fix / refactor / test / cleanup, keep 10-16 of the most promising). Return the slate in the same candidate schema.\n\nCANDIDATES (JSON):\n${JSON.stringify(rawCandidates)}`,
  { label: 'curate', phase: 'Scout', schema: CANDIDATE_SCHEMA }
)
const slate = (curated && curated.candidates) ? curated.candidates : rawCandidates
log(`Curated slate: ${slate.length} features`)

// ---- Phase 2: DESIGN ----
phase('Design')
const designs = await batched(slate, (c) =>
  agent(
    `You are a senior engineer designing ONE net-new MemoryMaster feature. Produce a concrete, buildable design spec grounded in the EXISTING stack (read the relevant modules first).\n\n${CONTEXT}\n\n` +
    `FEATURE: ${c.name} — ${c.one_liner}\n` +
    `Where it lives: ${c.where}\n` +
    `Status: ${c.status}. Why valuable: ${c.why_valuable}\n\n` +
    `Read the modules this would touch to ground the design in reality (don't invent APIs). Be concrete about the CLI/MCP/schema/dashboard/config surface, effort (S/M/L/XL), risks, how it differentiates vs mem0/Letta/Zep/claude-smart, a test plan, and dependencies. Read-only — design only, write no code.`,
    { label: `design:${c.name.slice(0, 30)}`, phase: 'Design', schema: DESIGN_SCHEMA, agentType: 'Explore' }
  ), BATCH)
const validDesigns = designs.filter(Boolean)
log(`Design: ${validDesigns.length} specs`)

// ---- Phase 3: JUDGE ---- 2 judges per design
phase('Judge')
const judgeTasks = []
for (const d of validDesigns) for (const lens of ['product-value', 'build-pragmatics']) judgeTasks.push({ d, lens })
const verdicts = await batched(judgeTasks, (t) =>
  agent(
    `Score this MemoryMaster feature design through the "${t.lens}" lens. Be a tough, honest judge — most features are 'maybe', few are 'build-now'.\n\n` +
    `DESIGN (JSON):\n${JSON.stringify({ name: t.d.name, category: t.d.category, problem: t.d.problem, design: t.d.design, surface: t.d.surface, effort: t.d.effort, risk: t.d.risk, differentiation: t.d.differentiation, dependencies: t.d.dependencies })}\n\n` +
    `Score value/effort_cost/risk/differentiation each 1-5 (see schema), total = sum, and give a verdict.`,
    { label: `judge:${t.d.name.slice(0, 22)}:${t.lens}`, phase: 'Judge', schema: JUDGE_SCHEMA }
  ), BATCH)

// Tally judges per design in code.
const scoreByName = new Map()
verdicts.forEach((v, i) => {
  if (!v) return
  const name = judgeTasks[i].d.name
  if (!scoreByName.has(name)) scoreByName.set(name, [])
  scoreByName.get(name).push(v)
})
const VORDER = { 'build-now': 3, strong: 2, maybe: 1, skip: 0 }
const ranked = validDesigns.map(d => {
  const vs = scoreByName.get(d.name) || []
  const avg = vs.length ? vs.reduce((a, b) => a + b.total, 0) / vs.length : 0
  const bestVerdict = vs.length ? vs.map(v => v.verdict).sort((a, b) => VORDER[b] - VORDER[a])[Math.floor(vs.length / 2)] : 'maybe'
  return { ...d, score: Math.round(avg * 10) / 10, verdict: bestVerdict, judge_notes: vs.map(v => v.rationale) }
}).sort((a, b) => b.score - a.score)

// ---- Phase 4: SYNTHESIZE ----
const SYNTH_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['executive_summary', 'build_first', 'themes'],
  properties: {
    executive_summary: { type: 'string' },
    build_first: { type: 'array', items: { type: 'string' } },
    themes: { type: 'array', items: { type: 'string' } },
  },
}
phase('Synthesize')
const synth = await agent(
  `Synthesize a MemoryMaster feature-R&D catalog from these scored designs. Give an executive_summary (the 2-3 features worth building first and why), and group the rest. Designs (JSON, ranked):\n${JSON.stringify(ranked.map(r => ({ name: r.name, category: r.category, effort: r.effort, score: r.score, verdict: r.verdict, problem: r.problem })))}`,
  { label: 'synthesize', phase: 'Synthesize', schema: SYNTH_SCHEMA }
)

return { stats: { raw: rawCandidates.length, slate: slate.length, designed: validDesigns.length }, synthesis: synth, catalog: ranked }
