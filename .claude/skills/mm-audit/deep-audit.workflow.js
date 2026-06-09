export const meta = {
  name: 'mm-deep-adversarial-audit',
  description: 'Multi-dimension finder sweep across MemoryMaster, dedup, then 3-skeptic adversarial refute-verify, then completeness critic',
  phases: [
    { title: 'Sweep', detail: 'dimension-specialist finders per module group' },
    { title: 'Dedup', detail: 'merge findings into a unique set' },
    { title: 'Verify', detail: '3 independent skeptics per finding, prompted to refute' },
    { title: 'Critic', detail: 'coverage-gap pass over what was missed' },
    { title: 'Synthesize', detail: 'executive rollup of confirmed findings' },
  ],
}

const REPO = 'G:/_OneDrive/OneDrive/Desktop/Py Apps/memorymaster'

// Server-side rate-limiting (Anthropic "temporarily limiting requests") cuts agents
// off mid-run when ~14 fire at once. Run in small sequential batches so only
// SWEEP_BATCH/VERIFY_BATCH agents are in flight at a time — gentler on the API and
// paces the burn. Each batch is a barrier; a thrown agent in a batch resolves to null.
const SWEEP_BATCH = 4
const VERIFY_BATCH = 6
async function batched(items, makeThunk, size) {
  const out = []
  for (let i = 0; i < items.length; i += size) {
    const slice = items.slice(i, i + size)
    const res = await parallel(slice.map((it, j) => () => makeThunk(it, i + j)))
    out.push(...res)
    log(`  batch ${Math.floor(i / size) + 1}/${Math.ceil(items.length / size)} done (${out.filter(Boolean).length}/${out.length} ok so far)`)
  }
  return out
}

// Project invariants finders must check against (from AGENTS.md + .claude/rules).
const INVARIANTS = [
  'The sensitivity filter MUST run on EVERY ingest path (mcp_server.ingest_claim, service.ingest, dream_bridge, rule_miner, any new path) — secrets/keys/private-IPs/tokens must never reach the claims DB. No allow_sensitive bypass may exist.',
  'SQL must be parameterized — never string-concatenate user/LLM-derived values into SQL.',
  'claim.status must only change through service.py / _storage_lifecycle.py helpers, never via direct UPDATE. Valid statuses are exactly the 6 in models.CLAIM_STATUSES.',
  'Supersession must set BOTH sides: new.supersedes_claim_id AND old.replaced_by_claim_id + status=superseded.',
  'SQLite and Postgres stores must stay at parity — schema, params (temporal/source/visibility), and behavior. A field added to one but not the other is a data-loss bug.',
  'WAL mode is mandatory for SQLite; concurrent access from the Stop hook + steward cycle + MCP must not corrupt or deadlock.',
  'Bitemporal fields (event_time, valid_from, valid_until) and idempotency_key dedup must be honored on ingest.',
  'LLM calls must respect llm_budget cycle caps; provider failures must not be miscounted as circuit-breaker failures.',
]

const DIMS = {
  correctness: 'Logic/correctness defects: wrong results, off-by-one, mishandled None/empty, broken error handling (bare except, swallowed errors), silent data drops on a specific code path, incorrect SQL, state machine violations, idempotency bugs.',
  concurrency: 'Concurrency & data-integrity: races/TOCTOU, non-atomic read-modify-write, transaction boundary errors, missing commit/rollback, WAL/locking issues under the Stop-hook + steward + MCP concurrent access, partial writes that corrupt invariants.',
  security: 'Security: sensitivity-filter bypasses or gaps on any ingest path, SQL injection / string-built SQL, path traversal, missing auth on dashboard/webhook, unsafe deserialization, secrets logged or persisted, HMAC/signature verification flaws.',
  perf: 'Performance & scale (DB is 2.5GB / 744k verbatim rows / 54k claims): N+1 queries, missing indexes, O(n^2) loops, full-table scans, non-sargable predicates, per-row connection/commit churn, unbounded fetches loaded into memory.',
  contract: 'API/contract: MCP tool contracts (auto-citation fallback, source_agent, sensitivity wrapper), SQLite<->Postgres parity divergence, CLI JSON-envelope shape drift, schema/migration mismatches, public-API signature inconsistencies.',
}

const GROUPS = [
  { name: 'core-service', dims: ['correctness', 'concurrency', 'contract'],
    modules: ['service.py', 'models.py', 'config.py', 'scope_utils.py', 'retry.py'] },
  { name: 'storage-sqlite', dims: ['correctness', 'concurrency', 'perf'],
    modules: ['storage.py', '_storage_read.py', '_storage_write_claims.py', '_storage_schema.py', '_storage_lifecycle.py', '_storage_meta.py', '_storage_sources.py', '_storage_shared.py', 'storage_maintenance.py', 'lifecycle.py'] },
  { name: 'storage-backends', dims: ['correctness', 'concurrency', 'contract', 'perf'],
    modules: ['postgres_store.py', 'db_merge.py', 'delta_sync.py', 'delta_compress.py', 'delta_sync_state.py', 'snapshot.py', 'migrations/runner.py'] },
  { name: 'mcp-api', dims: ['security', 'contract', 'correctness'],
    modules: ['mcp_server.py', 'mcp_path_policy.py', 'mcp_usage.py', 'context_hook.py', 'hook_log.py', 'session_tracker.py'] },
  { name: 'steward-lifecycle', dims: ['correctness', 'concurrency'],
    modules: ['llm_steward.py', 'steward.py', 'steward_features.py', 'steward_classifier.py', 'conflict_resolver.py', 'auto_resolver.py', 'contradiction_probe.py', 'candidate_dedupe.py'] },
  { name: 'retrieval', dims: ['correctness', 'perf'],
    modules: ['retrieval.py', 'recall_fusion.py', 'recall_tokenizer.py', 'query_cache.py', 'query_expansion.py', 'query_classifier.py', 'qdrant_backend.py', 'qdrant_recall_fallback.py', 'verbatim_recall.py', 'context_optimizer.py', 'embeddings.py'] },
  { name: 'ingest-mining', dims: ['security', 'correctness', 'perf'],
    modules: ['verbatim_store.py', 'verbatim_cleanup.py', 'rule_miner.py', 'dream_bridge.py', 'auto_extractor.py', 'entity_registry.py', 'entity_extractor.py', 'entity_graph.py', 'claim_edges.py', 'atlas_claim_extractor.py'] },
  { name: 'wiki-vault', dims: ['correctness', 'contract'],
    modules: ['wiki_engine.py', 'wiki_freshness.py', 'wiki_suggest.py', 'wiki_similarity.py', 'wiki_validate.py', 'vault_linter.py', 'vault_curator.py', 'vault_synthesis.py', 'vault_exporter.py', 'vault_query_capture.py', 'daily_notes.py'] },
  { name: 'llm-providers', dims: ['correctness', 'concurrency', 'security'],
    modules: ['llm_provider.py', 'llm_budget.py', 'key_rotator.py', 'llm_rerank.py'] },
  { name: 'security-access', dims: ['security', 'correctness'],
    modules: ['security.py', 'access_control.py', 'dashboard_auth.py', 'webhook.py'] },
  { name: 'cli', dims: ['correctness', 'contract'],
    modules: ['cli.py', 'cli_handlers_basic.py', 'cli_handlers_curation.py', 'cli_handlers_meta.py', 'cli_helpers.py'] },
]

const FINDINGS_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['findings'],
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['file', 'location', 'dimension', 'severity', 'title', 'description', 'why_it_is_a_bug', 'trigger', 'suggested_fix', 'confidence'],
        properties: {
          file: { type: 'string', description: 'memorymaster/<module>.py' },
          location: { type: 'string', description: 'function/method/class name + line number(s)' },
          dimension: { type: 'string', enum: ['correctness', 'concurrency', 'security', 'perf', 'contract'] },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          title: { type: 'string' },
          description: { type: 'string' },
          why_it_is_a_bug: { type: 'string', description: 'the concrete wrong behavior / broken invariant — NOT style preference' },
          trigger: { type: 'string', description: 'the input/condition/sequence that exercises it' },
          suggested_fix: { type: 'string' },
          confidence: { type: 'number', description: '0..1 — your confidence this is a genuine defect' },
        },
      },
    },
  },
}

const DEDUP_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['unique'],
  properties: {
    unique: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['id', 'file', 'location', 'dimension', 'severity', 'title', 'description', 'why_it_is_a_bug', 'trigger', 'suggested_fix', 'merged_from'],
        properties: {
          id: { type: 'integer' },
          file: { type: 'string' }, location: { type: 'string' },
          dimension: { type: 'string' }, severity: { type: 'string' },
          title: { type: 'string' }, description: { type: 'string' },
          why_it_is_a_bug: { type: 'string' }, trigger: { type: 'string' }, suggested_fix: { type: 'string' },
          merged_from: { type: 'integer', description: 'how many raw findings collapsed into this one' },
        },
      },
    },
  },
}

const VERDICT_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['verdict', 'severity_corrected', 'reasoning'],
  properties: {
    verdict: { type: 'string', enum: ['real', 'refuted', 'uncertain'], description: 'real = confirmed genuine defect; refuted = not a bug (guarded elsewhere / intended / misread); uncertain = cannot confirm from the code' },
    severity_corrected: { type: 'string', enum: ['critical', 'high', 'medium', 'low', 'none'] },
    reasoning: { type: 'string', description: 'cite the specific code you read that supports your verdict' },
  },
}

const SYNTH_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['executive_summary', 'top_risks', 'themes'],
  properties: {
    executive_summary: { type: 'string', description: '4-8 sentences: overall health, where the real risk concentrates' },
    top_risks: { type: 'array', items: { type: 'string', description: 'the most important confirmed issues, most severe first' } },
    themes: { type: 'array', items: { type: 'string', description: 'recurring patterns across findings (e.g. a silent-dropper repeated in N places)' } },
  },
}

// ---- Phase 1: SWEEP ----
phase('Sweep')
const cells = []
for (const g of GROUPS) for (const d of g.dims) cells.push({ group: g, dim: d })
log(`Sweep: ${cells.length} finder cells across ${GROUPS.length} module groups`)

const rawBatches = await batched(cells, (c) =>
  agent(
    `You are a senior code auditor reviewing MemoryMaster (Python memory-reliability system) at ${REPO}.\n\n` +
    `AUDIT LENS: ${c.dim.toUpperCase()} — ${DIMS[c.dim]}\n\n` +
    `MODULE GROUP "${c.group.name}". Read these files IN FULL (skip any that don't exist):\n` +
    c.group.modules.map(m => `  memorymaster/${m}`).join('\n') + '\n\n' +
    `Project invariants — a violation of any is a real finding; check against them:\n` +
    INVARIANTS.map((s, i) => `  ${i + 1}. ${s}`).join('\n') + '\n\n' +
    `Report ONLY genuine defects through the ${c.dim} lens. Each finding needs file, location (symbol + line#), the concrete wrong behavior (why_it_is_a_bug), the trigger, and a suggested fix.\n` +
    `STRICT QUALITY BAR: no style nits, no "could be cleaner", no speculative "might". If you cannot point at a concrete way it produces wrong/unsafe/slow behavior, do not report it. It is correct to return an empty findings list for a clean group. Set confidence honestly (lower if you could not fully trace it). Read the actual code — do not guess from names.`,
    { label: `sweep:${c.group.name}:${c.dim}`, phase: 'Sweep', schema: FINDINGS_SCHEMA }
  ),
  SWEEP_BATCH)

const raw = rawBatches.filter(Boolean).flatMap(b => b.findings || [])
log(`Sweep: ${raw.length} raw findings from ${rawBatches.filter(Boolean).length}/${cells.length} finders`)

if (raw.length === 0) {
  return { confirmed: [], note: 'Sweep returned zero findings.' }
}

// ---- Phase 2: DEDUP ---- (single agent; findings are short text)
phase('Dedup')
const dedupRes = await agent(
  `These are raw audit findings for MemoryMaster from many independent finders. Merge duplicates/near-duplicates ` +
  `(same file + same root cause, even if titled differently) into a single unique list. Assign each a sequential integer id starting at 1. ` +
  `Keep the clearest description and the highest justified severity when merging; set merged_from to the count collapsed. Do NOT invent new findings, do NOT drop a finding just because it is low severity.\n\n` +
  `RAW FINDINGS (JSON):\n${JSON.stringify(raw)}`,
  { label: 'dedup', phase: 'Dedup', schema: DEDUP_SCHEMA }
)
const unique = (dedupRes && dedupRes.unique) ? dedupRes.unique : []
log(`Dedup: ${raw.length} raw -> ${unique.length} unique findings`)

// ---- Phase 3: VERIFY ---- 3 skeptics per finding, prompted to refute
phase('Verify')
const LENSES = ['does-it-reproduce', 'is-it-guarded-elsewhere', 'is-severity-right']
// Flatten to one task per (finding, lens) so batched() can pace ALL skeptics, not just findings.
const verifyTasks = []
for (const f of unique) for (const lens of LENSES) verifyTasks.push({ f, lens })
const verdictResults = await batched(verifyTasks, (t) =>
  agent(
    `You are an adversarial verifier for a MemoryMaster audit finding. Your DEFAULT stance is skeptical: try to REFUTE it. ` +
    `Only return verdict="real" if, after reading the actual code, you are convinced it is a genuine defect. Verdict="refuted" if it is guarded elsewhere, intended behavior, or a misread. Verdict="uncertain" if you cannot confirm from the code.\n\n` +
    `Verification lens: ${t.lens}.\n\n` +
    `FINDING:\n` +
    `  file: ${t.f.file}\n  location: ${t.f.location}\n  dimension: ${t.f.dimension}\n  claimed severity: ${t.f.severity}\n` +
    `  title: ${t.f.title}\n  why_it_is_a_bug: ${t.f.why_it_is_a_bug}\n  trigger: ${t.f.trigger}\n\n` +
    `Open ${REPO}/${t.f.file} at the cited location, read the surrounding code AND any caller/guard that could invalidate the claim. ` +
    `Base your verdict on code you actually read, and cite it in reasoning.`,
    { label: `verify:${t.f.id}:${t.lens}`, phase: 'Verify', schema: VERDICT_SCHEMA }
  ),
  VERIFY_BATCH)
// Regroup verdicts by finding id.
const votesById = new Map()
verdictResults.forEach((v, idx) => {
  if (!v) return
  const fid = verifyTasks[idx].f.id
  if (!votesById.has(fid)) votesById.set(fid, [])
  votesById.get(fid).push(v)
})
const verified = unique.map(f => ({ finding: f, votes: votesById.get(f.id) || [] }))

// Deterministic consensus in code (no model for tallying).
const SEV_RANK = { critical: 4, high: 3, medium: 2, low: 1, none: 0 }
const SEV_NAME = ['none', 'low', 'medium', 'high', 'critical']
const confirmed = []
const dropped = []
for (const v of verified.filter(Boolean)) {
  const votes = v.votes
  if (votes.length === 0) { dropped.push({ ...v.finding, drop_reason: 'no verifier returned' }); continue }
  const real = votes.filter(x => x.verdict === 'real').length
  const refuted = votes.filter(x => x.verdict === 'refuted').length
  // confirmed = majority did NOT refute AND at least one says real
  const isConfirmed = refuted < Math.ceil(votes.length / 2) && real >= 1
  if (isConfirmed) {
    // consensus severity = median-ish: take the max corrected severity that >=1 verifier assigned, capped at claimed
    const sevVals = votes.map(x => SEV_RANK[x.severity_corrected] ?? SEV_RANK[v.finding.severity] ?? 1).filter(n => n > 0)
    const consensusSev = sevVals.length ? SEV_NAME[Math.round(sevVals.reduce((a, b) => a + b, 0) / sevVals.length)] : v.finding.severity
    confirmed.push({ ...v.finding, severity: consensusSev, votes: { real, refuted, uncertain: votes.length - real - refuted }, verifier_notes: votes.map(x => x.reasoning) })
  } else {
    dropped.push({ ...v.finding, votes: { real, refuted, uncertain: votes.length - real - refuted }, drop_reason: 'majority refuted or none confirmed' })
  }
}
confirmed.sort((a, b) => (SEV_RANK[b.severity] - SEV_RANK[a.severity]) || (b.votes.real - a.votes.real))
log(`Verify: ${confirmed.length} confirmed, ${dropped.length} dropped (of ${unique.length} unique)`)

// ---- Phase 4: CRITIC ---- coverage gaps
phase('Critic')
const critic = await agent(
  `You are a completeness critic for a MemoryMaster security/correctness audit. ` +
  `The sweep covered these (group:dimension) cells:\n${cells.map(c => `${c.group.name}:${c.dim}`).join(', ')}\n\n` +
  `It produced ${unique.length} unique findings, ${confirmed.length} confirmed after adversarial verification. ` +
  `Here are the confirmed finding titles:\n${confirmed.map(f => `- [${f.severity}] ${f.file}: ${f.title}`).join('\n') || '(none)'}\n\n` +
  `Identify GAPS: which modules, code paths, or risk classes were likely under-examined or not covered by any cell? ` +
  `Think about: cross-module data flows, the hook scripts under config_templates/hooks, the migrations themselves, anything not in the group list. ` +
  `Return a short prose assessment of what a follow-up round should target. Be specific (name files/paths). Do NOT re-list the findings above.`,
  { label: 'completeness-critic', phase: 'Critic' }
)

// ---- Phase 5: SYNTHESIZE ----
phase('Synthesize')
const synth = await agent(
  `Synthesize the executive view of this MemoryMaster audit. ${confirmed.length} confirmed findings after 3-skeptic adversarial verification.\n\n` +
  `CONFIRMED FINDINGS (JSON):\n${JSON.stringify(confirmed.map(f => ({ file: f.file, location: f.location, dimension: f.dimension, severity: f.severity, title: f.title, why: f.why_it_is_a_bug })))}\n\n` +
  `Write a tight executive_summary (overall health + where real risk concentrates), an ordered top_risks list (most severe/load-bearing first), and themes (recurring patterns — e.g. a silent-dropper or missing-guard repeated across modules).`,
  { label: 'synthesize', phase: 'Synthesize', schema: SYNTH_SCHEMA }
)

return {
  stats: { cells: cells.length, raw: raw.length, unique: unique.length, confirmed: confirmed.length, dropped: dropped.length },
  synthesis: synth,
  completeness_gaps: critic,
  confirmed,
  dropped,
}
