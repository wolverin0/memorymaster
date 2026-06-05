export const meta = {
  name: 'mm-mine-corrections',
  description: 'Mine the verbatim corpus for recurring user corrections, cluster them, and propose evidence-linked skills/rules',
  phases: [
    { title: 'Harvest', detail: 'pull correction windows from verbatim (deterministic SQL)' },
    { title: 'Extract', detail: 'parallel agents distill each window to a structured correction' },
    { title: 'Cluster', detail: 'group recurring corrections into themes' },
    { title: 'Propose', detail: 'turn high-frequency themes into skill/rule proposals with evidence' },
  ],
}

const REPO = 'G:/_OneDrive/OneDrive/Desktop/Py Apps/memorymaster'
const DB = `${REPO}/memorymaster.db`
const BATCH = 6
// args: { limit?: number, scope?: string }  — how many correction windows to harvest, optional scope filter
const LIMIT = (args && args.limit) || 120
const SCOPE = (args && args.scope) || null

async function batched(items, makeThunk, size) {
  const out = []
  for (let i = 0; i < items.length; i += size) {
    const res = await parallel(items.slice(i, i + size).map((it, j) => () => makeThunk(it, i + j)))
    out.push(...res)
    log(`  batch ${Math.floor(i / size) + 1}/${Math.ceil(items.length / size)} done`)
  }
  return out
}

// ---- Phase 1: HARVEST (deterministic — a Python agent runs SQL over verbatim) ----
// We do NOT load 857k rows into context. A single agent runs a bounded SQL query
// that mirrors rule_miner's correction pre-filter (FTS5 MATCH / LIKE on correction
// keywords over role='user' turns) and returns up to LIMIT windows as JSON:
// each window = { user_id, user_text, assistant_text, scope, session_id }.
phase('Harvest')
const HARVEST_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['windows'],
  properties: { windows: { type: 'array', items: {
    type: 'object', additionalProperties: false,
    required: ['user_id', 'user_text', 'assistant_text', 'scope'],
    properties: {
      user_id: { type: 'integer' },
      user_text: { type: 'string' },
      assistant_text: { type: 'string' },
      scope: { type: 'string' },
    } } } },
}
const harvest = await agent(
  `You are harvesting correction windows from the MemoryMaster verbatim corpus for analysis. ` +
  `Write and run a Python script (sqlite3, READ-ONLY mode=ro) against the DB at ${DB}.\n\n` +
  `Goal: find up to ${LIMIT} USER turns that look like CORRECTIONS of the assistant ("no, do X instead", "don't", "actually", "wrong", "revert", etc.), each paired with the immediately PRECEDING assistant turn in the same session.\n\n` +
  `Use memorymaster.rule_miner's own pre-filter as the source of truth — import _CORRECTION_KEYWORDS (and _CORRECTION_FTS_MATCH if FTS is available) from memorymaster.rule_miner so the keyword set matches production. Query verbatim_memories (columns: id, session_id, role, content, scope, timestamp). For each matching user row, fetch the preceding assistant row: WHERE session_id=? AND id<? AND role='assistant' ORDER BY id DESC LIMIT 1; skip windows with no preceding assistant turn. ${SCOPE ? `Filter to scope='${SCOPE}'.` : 'All scopes.'}\n\n` +
  `Truncate user_text and assistant_text to ~1500 chars each. Return up to ${LIMIT} windows. Read-only — do not write the DB. If FTS isn't available, fall back to the LIKE pre-filter. Return the structured windows.`,
  { label: 'harvest', phase: 'Harvest', schema: HARVEST_SCHEMA }
)
const windows = (harvest && harvest.windows) ? harvest.windows : []
log(`Harvest: ${windows.length} correction windows`)
if (windows.length === 0) return { note: 'No correction windows found.', proposals: [] }

// ---- Phase 2: EXTRACT (parallel: one agent per window -> structured correction) ----
phase('Extract')
const CORR_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['is_correction', 'trigger', 'instruction', 'rationale', 'theme'],
  properties: {
    is_correction: { type: 'boolean', description: 'true only if the user turn genuinely corrects assistant behavior (not praise/new-task/question)' },
    trigger: { type: 'string', description: 'WHEN this applies — the situation that prompted the correction (empty if not a correction)' },
    instruction: { type: 'string', description: 'WHAT to do instead — the prescriptive rule' },
    rationale: { type: 'string', description: 'WHY — the reason, if stated or clearly implied' },
    theme: { type: 'string', description: 'a short kebab-case theme tag, e.g. "no-fabrication", "verify-before-claim", "minimal-diffs"' },
  },
}
const extracted = await batched(windows, (w, i) =>
  agent(
    `Analyze ONE exchange between an AI coding assistant and its user. Decide if the USER turn is a CORRECTION of the assistant's behavior, and if so distill it to a prescriptive rule.\n\n` +
    `ASSISTANT said:\n"""${w.assistant_text}"""\n\nUSER replied:\n"""${w.user_text}"""\n\n` +
    `If the user is correcting/redirecting the assistant ("no, do X instead", "don't", "actually", "you fabricated", "verify first", etc.), set is_correction=true and fill trigger/instruction/rationale/theme. ` +
    `If it's praise, a new task, a question, or unrelated, set is_correction=false and leave the rest empty. ` +
    `Do NOT include any secrets/tokens/paths in the output. Be concise and prescriptive.`,
    { label: `extract:${i}`, phase: 'Extract', schema: CORR_SCHEMA, model: 'haiku' }
  ), BATCH)

// attach provenance (source verbatim id + scope) to each real correction
const corrections = []
extracted.forEach((c, i) => {
  if (c && c.is_correction && c.instruction) {
    corrections.push({ ...c, source_verbatim_id: windows[i].user_id, scope: windows[i].scope })
  }
})
log(`Extract: ${corrections.length} genuine corrections (of ${windows.length} windows)`)
if (corrections.length === 0) return { note: 'No genuine corrections after extraction.', proposals: [] }

// ---- Phase 3: CLUSTER (one agent: group recurring corrections into themes) ----
phase('Cluster')
const CLUSTER_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['clusters'],
  properties: { clusters: { type: 'array', items: {
    type: 'object', additionalProperties: false,
    required: ['theme', 'count', 'canonical_rule', 'member_verbatim_ids', 'example_instructions'],
    properties: {
      theme: { type: 'string' },
      count: { type: 'integer', description: 'how many corrections fall in this cluster' },
      canonical_rule: { type: 'string', description: 'the merged prescriptive rule (trigger -> instruction -> rationale)' },
      member_verbatim_ids: { type: 'array', items: { type: 'integer' }, description: 'source_verbatim_id of each member (evidence)' },
      example_instructions: { type: 'array', items: { type: 'string' } },
    } } } },
}
const clustered = await agent(
  `Group these extracted user corrections into RECURRING THEMES. Merge near-duplicates; a theme is valuable only if it recurs (count >= 2 is interesting, >= 3 is strong). For each cluster give a single canonical prescriptive rule, the count, the member source_verbatim_ids (evidence), and 2-3 example instructions. Rank clusters by count desc.\n\n` +
  `CORRECTIONS (JSON):\n${JSON.stringify(corrections.map(c => ({ id: c.source_verbatim_id, theme: c.theme, trigger: c.trigger, instruction: c.instruction, rationale: c.rationale, scope: c.scope })))}`,
  { label: 'cluster', phase: 'Cluster', schema: CLUSTER_SCHEMA }
)
const clusters = (clustered && clustered.clusters) ? clustered.clusters.filter(c => c.count >= 2) : []
log(`Cluster: ${clusters.length} recurring themes (count>=2)`)

// ---- Phase 4: PROPOSE (turn the top themes into skill/rule proposals) ----
phase('Propose')
const PROPOSAL_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['theme', 'recommendation', 'name', 'where', 'body', 'why_skill_not_claude_md'],
  properties: {
    theme: { type: 'string' },
    recommendation: { type: 'string', enum: ['skill', 'claude-md-rule', 'mm-rule-claim', 'drop'], description: 'best home for this correction. Prefer "skill" for behavioral patterns; "mm-rule-claim" for project-scoped prescriptive facts; "claude-md-rule" only for terse always-on workstyle; "drop" if too noisy/one-off' },
    name: { type: 'string', description: 'kebab-case skill/rule name' },
    where: { type: 'string', description: 'concrete path it would live (e.g. .claude/skills/<name>/SKILL.md or .claude/rules/<name>.md or an ingest_rule call)' },
    body: { type: 'string', description: 'the actual proposed rule/skill text (prescriptive, trigger/action/rationale shape)' },
    why_skill_not_claude_md: { type: 'string', description: 'one line: why this home vs bloating CLAUDE.md' },
  },
}
const proposals = await batched(clusters, (cl) =>
  agent(
    `Turn this recurring correction theme into a concrete proposal. Per the dynamic-workflows guidance, prefer packaging behavioral corrections as SKILLS or MemoryMaster rule-claims rather than bloating CLAUDE.md. Decide the best home (recommendation) and write the actual rule/skill body in a prescriptive trigger->action->rationale shape.\n\n` +
    `THEME: ${cl.theme} (recurred ${cl.count}x)\nCanonical rule: ${cl.canonical_rule}\nExamples: ${JSON.stringify(cl.example_instructions)}\nEvidence verbatim ids: ${JSON.stringify(cl.member_verbatim_ids)}`,
    { label: `propose:${cl.theme}`, phase: 'Propose', schema: PROPOSAL_SCHEMA }
  ), BATCH)

const final = proposals.filter(Boolean).map((p, i) => ({ ...p, count: clusters[i] ? clusters[i].count : null, evidence_verbatim_ids: clusters[i] ? clusters[i].member_verbatim_ids : [] }))
  .filter(p => p.recommendation !== 'drop')
  .sort((a, b) => (b.count || 0) - (a.count || 0))

return {
  stats: { windows: windows.length, corrections: corrections.length, clusters: clusters.length, proposals: final.length },
  proposals: final,
}
