export const meta = {
  name: 'mm4-reliability-design',
  description: 'MM v4 P1-WF1: judge panel on the single-writer architecture (daemon vs write-broker vs WAL-hardening-only) -> implementable spec',
  whenToUse: 'Phase 1 design stage of the MemoryMaster v4 consolidation program.',
  phases: [
    { title: 'Draft', detail: '3 independent architecture drafts from different priors' },
    { title: 'Judge', detail: '3 judges score all drafts on distinct lenses' },
    { title: 'Spec', detail: 'synthesize the winning design into a build spec' },
  ],
}

const REPO = 'G:/_OneDrive/OneDrive/Desktop/Py Apps/memorymaster'

const CONTEXT =
  `Repo: ${REPO} (MemoryMaster v3.28.0, Python 3.10+, SQLite WAL, FastMCP stdio server). ` +
  `PROBLEM: ~12 concurrent writer processes share one 3.2GB SQLite file — one mcp_server per Claude Code pane (stdio MCP), ` +
  `plus Stop/recall hooks, a 6h scheduled steward (pythonw), dream bridge, and OpenClaw sync. Real btree corruption occurred ` +
  `2026-06-05 (salvage scripts in scripts/, artifact memorymaster.db.corrupt-2026-06-05 in tree). v3.27 already shipped ` +
  `busy_timeout + WAL hardening; writer COUNT is unchanged. Constraints that matter: Windows 10 host; per-pane MCP servers are ` +
  `spawned BY Claude Code from .claude.json config (user cannot easily change how panes spawn them); the recall hook fires on ` +
  `EVERY user prompt as a fresh process (~1.2s cold tax measured, baseline .planning/BASELINE-2026-06-09.html); cold init_db ` +
  `on the 3GB DB is 16.06s; reads must stay fast (recall p50 70ms). Read the actual code before claiming anything: ` +
  `memorymaster/mcp_server.py, service.py (ingest paths), storage.py + _storage_*.py (connection handling), retry.py, ` +
  `context_hook.py (hook read path), scripts/ (corruption history). Cite file:line for every claim about current behavior. ` +
  `Do NOT modify any files. Do NOT open the live DB for writes.`

const DRAFT_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['name', 'summary', 'components', 'migration_path', 'failure_modes', 'latency_impact', 'windows_ergonomics', 'effort_estimate', 'evidence'],
  properties: {
    name: { type: 'string' },
    summary: { type: 'string', description: '5-10 sentence design summary' },
    components: { type: 'array', items: { type: 'string' }, description: 'each component with its responsibility' },
    migration_path: { type: 'string', description: 'how 12 existing writers move over, with rollback story' },
    failure_modes: { type: 'array', items: { type: 'string' }, description: 'what breaks when the new piece dies, and the blast radius' },
    latency_impact: { type: 'string', description: 'effect on recall p50/p95 and ingest latency, reasoned from the measured baseline' },
    windows_ergonomics: { type: 'string', description: 'how it runs on Windows 10: service/task/daemon story, restart-on-boot, no-admin constraints' },
    effort_estimate: { type: 'string', description: 'modules touched + new code size, honest' },
    evidence: { type: 'array', items: { type: 'string' }, description: 'file:line citations from the real code that ground this design' },
  },
}

phase('Draft')
const PRIORS = [
  { key: 'broker', prior: 'You believe the WRITE-BROKER is right: reads stay direct (SQLite handles concurrent readers fine in WAL), only WRITES serialize through one owner process reached over localhost (named pipe / TCP on 127.0.0.1 / filesystem queue — argue the transport). Smallest migration: service.ingest and other mutating paths route to the broker when MEMORYMASTER_WRITE_BROKER=1, fall back to direct writes when the broker is down (availability over strict serialization) OR fail closed (argue which). The broker is a tiny stdlib process, auto-started on demand with a singleton lock.' },
  { key: 'daemon', prior: 'You believe a FULL DAEMON is right: ONE long-lived MemoryMaster server owns the DB entirely (reads AND writes); per-pane mcp_server becomes a thin stdio shim proxying to it; hooks call it over localhost instead of importing the library. This also kills the measured 1.2s/prompt hook cold tax and the 16s cold init (daemon pays once). Bigger migration but the end-state the project actually wants. Address: lifecycle on Windows (start on boot, restart on crash), what happens to panes when the daemon is down, and version-skew between shim and daemon.' },
  { key: 'minimal', prior: 'You believe NO NEW PROCESS is right: SQLite in WAL mode with busy_timeout, properly used, supports N writers safely — the 2026-06-05 corruption needs a proven root cause before adding moving parts. Investigate what ACTUALLY corrupts: candidates include the DB living under a OneDrive-synced path (check directory), kill -9 / power events mid-checkpoint, multiple sqlite versions, missing fsync, WAL file deletion while open. Propose: hardening checklist (immediate), scheduled quick_check + VACUUM INTO snapshots, write-rate reduction (hooks batch through a queue file), and the evidence that would justify escalating to a broker later.' },
]
const drafts = await parallel(PRIORS.map((p) => () =>
  agent(
    `${CONTEXT}\n\nROLE: senior systems engineer drafting the P1 single-writer architecture for this codebase. PRIOR YOU ARGUE FROM: ${p.prior}\n` +
    `Steelman YOUR approach honestly against the constraints above — but ground every claim in the actual code (cite file:line). ` +
    `Deliver an implementable design, not a vision statement.`,
    { label: `draft:${p.key}`, phase: 'Draft', schema: DRAFT_SCHEMA })))

const valid = drafts.filter(Boolean)
log(`${valid.length}/3 drafts returned`)

phase('Judge')
const JUDGE_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['scores', 'verdict', 'reasoning'],
  properties: {
    scores: { type: 'object', additionalProperties: false, required: ['broker', 'daemon', 'minimal'],
      properties: { broker: { type: 'number' }, daemon: { type: 'number' }, minimal: { type: 'number' } },
      description: '0-10 per design on YOUR lens only' },
    verdict: { type: 'string', enum: ['broker', 'daemon', 'minimal'] },
    reasoning: { type: 'string', description: 'why, grounded in the drafts AND the real code/constraints' },
  },
}
const LENSES = [
  { key: 'migration-risk', lens: 'Migration risk & reversibility: which design can ship incrementally behind a flag, roll back by unsetting an env var, and never strands the 12 production writers mid-migration? Penalize big-bang cutovers and version-skew traps.' },
  { key: 'failure-modes', lens: 'Failure modes & corruption: which design most credibly RETIRES the corruption class (not just shrinks it)? Walk each design through: new process crashes mid-write, machine powers off, two instances race at startup, DB file locked by antivirus/backup. Penalize designs that add new single points of failure without supervision stories.' },
  { key: 'ops-and-latency', lens: 'Windows operability & latency: which design is honest about running unattended on a Windows 10 desktop (no admin service manager, OneDrive paths, scheduled tasks) and protects the measured recall p50 70ms / fixes the 1.2s hook tax and 16s cold init where it claims to?' },
]
const judges = await parallel(LENSES.map((l) => () =>
  agent(
    `${CONTEXT}\n\nROLE: adversarial design judge. YOUR LENS (score ONLY this): ${l.lens}\n\nTHE THREE DRAFTS:\n${JSON.stringify(valid)}\n\n` +
    `Score each 0-10 on your lens, pick a verdict, justify. Spot-check the drafts' file:line citations against the real code — a draft that misrepresents current behavior loses points.`,
    { label: `judge:${l.key}`, phase: 'Judge', schema: JUDGE_SCHEMA })))

phase('Spec')
const SPEC_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['chosen', 'tally', 'spec_path', 'build_steps', 'test_plan', 'rollout', 'open_risks'],
  properties: {
    chosen: { type: 'string', enum: ['broker', 'daemon', 'minimal', 'hybrid'] },
    tally: { type: 'string', description: 'judge votes + score totals' },
    spec_path: { type: 'string' },
    build_steps: { type: 'array', items: { type: 'string' }, description: 'ordered, each independently testable, each names the files it touches' },
    test_plan: { type: 'string', description: 'unit tests + the chaos-soak design (12 simulated writers, kill -9 rounds, quick_check after each)' },
    rollout: { type: 'string', description: 'flag name, default state, dogfood period, flip criteria, rollback' },
    open_risks: { type: 'array', items: { type: 'string' } },
  },
}
const spec = await agent(
  `${CONTEXT}\n\nROLE: principal engineer synthesizing the P1 build spec.\n\nDRAFTS:\n${JSON.stringify(valid)}\n\nJUDGE VERDICTS:\n${JSON.stringify(judges.filter(Boolean))}\n\n` +
  `Pick the winner by judge tally (break ties toward lower migration risk). Graft in the best ideas from the losers — especially 'minimal''s hardening checklist and root-cause investigation items, which are cheap and compose with anything. ` +
  `Write the full build spec as markdown to ${REPO}/.planning/P1-RELIABILITY-SPEC.md: architecture, components, wire protocol if any, ordered build steps with file lists, test plan incl. the chaos soak, rollout plan behind an env flag with rollback, and the scheduled-integrity additions (quick_check + wal_checkpoint + VACUUM INTO snapshot as a steward phase) plus the 401-orphan-FK repair and the Qdrant reconciliation job — those three ship in P1 regardless of the architecture verdict. ` +
  `Steps must be sized so a single builder agent can complete each one in the MAIN checkout sequentially (editable-install import pin: worktree pytest lies). Return the structured summary.`,
  { label: 'synthesize-spec', phase: 'Spec', schema: SPEC_SCHEMA })

return { drafts: valid.map(d => ({ name: d.name, summary: d.summary })), judges: judges.filter(Boolean), spec }
