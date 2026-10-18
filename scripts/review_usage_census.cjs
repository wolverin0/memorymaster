'use strict';
// T-1343 AC4: retained from wezbridge/_intel/evidence/T-1344/census.cjs.
// Read-only transcript census; outputs counts and profile server names only.
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const readline = require('readline');
const os = require('os');
const { parseArgs } = require('util');

const MM_ID = /\[mm-[A-Za-z0-9]+\]/g;
const PFX = 'G---OneDrive-OneDrive-Desktop-Py-Apps-';

function laneOf(slug) {
  const rules = [
    ['wezbridge', new RegExp('^' + PFX + 'wezbridge(-|$)')],
    ['memorymaster', new RegExp('^' + PFX + 'memorymaster(-|$)')],
    ['infra', new RegExp('^' + PFX + 'infra(-|$)')],
    ['yolo26', new RegExp('^' + PFX + 'yolo26(-|$)')],
    ['futuramax', new RegExp('^' + PFX + '(futuraMAX|--worktrees-futuramax)')],
    ['wabot2', new RegExp('^' + PFX + '--worktrees-wabot2-main$')],
    ['wisp', new RegExp('^' + PFX + '(whatsappbot-main-wt|whatsappbot-prod---Copy---Copy-whatsappbot-final|--worktrees-bot-rf-optimizer)')],
    ['pedrito', new RegExp('^' + PFX + 'pedrito(-|$)')],
  ];
  for (const [lane, re] of rules) if (re.test(slug)) return lane;
  return 'other:external'; // Never expose transcript directory names or personal paths.
}

function listFiles(root, since) {
  const out = [];
  for (const slug of fs.readdirSync(root).sort()) {
    const sdir = path.join(root, slug);
    if (!fs.statSync(sdir).isDirectory()) continue;
    const walk = (dir, depth) => {
      for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
        const p = path.join(dir, e.name);
        if (e.isDirectory() && depth < 2) walk(p, depth + 1);
        else if (e.isFile() && e.name.endsWith('.jsonl')) {
          const st = fs.statSync(p);
          if (st.mtimeMs >= since) out.push({ slug, file: p });
        }
      }
    };
    walk(sdir, 0);
  }
  return out;
}

function blocksOf(entry) {
  const c = entry && entry.message && entry.message.content;
  if (typeof c === 'string') return [{ type: 'text', text: c }];
  return Array.isArray(c) ? c : [];
}

const newAgg = () => ({ tool_calls: 0, auto_recall: 0, injected_ids: 0, citations: 0, cited_of_injected: 0,
  deliberate: 0, deliberate_by_tool: {}, ingest: 0, gitnexus: 0 });
const SUM_KEYS = ['tool_calls', 'auto_recall', 'injected_ids', 'citations', 'cited_of_injected', 'deliberate', 'ingest', 'gitnexus'];

async function census({ root, end, profilesSnapshot }) {
  const until = Date.parse(end);
  if (!Number.isFinite(until)) throw new Error('A valid --end timestamp is required');
  const since = until - 7 * 24 * 3600 * 1000;
  const files = listFiles(root, since);
  const agg = {};
  const typeCounts = {};
  const excluded = { missing_or_invalid_timestamp: 0, outside_window: 0 };
  const sha = crypto.createHash('sha256')
    .update(fs.readFileSync(__filename, 'utf8').replace(/\r\n/g, '\n')).digest('hex');
  const perLane = {};
  const cell = (lane, day) => {
    agg[lane] = agg[lane] || {};
    return (agg[lane][day] = agg[lane][day] || newAgg());
  };
  for (const f of files) {
    const lane = laneOf(f.slug);
    perLane[lane] = (perLane[lane] || 0) + 1;
    const injected = new Set();
    const rl = readline.createInterface({ input: fs.createReadStream(f.file), crlfDelay: Infinity });
    for await (const line of rl) {
      if (!line.trim()) continue;
      let entry;
      try { entry = JSON.parse(line); } catch (e) { continue; }
      const timestamp = typeof entry.timestamp === 'string' ? Date.parse(entry.timestamp) : NaN;
      if (!Number.isFinite(timestamp)) { excluded.missing_or_invalid_timestamp++; continue; }
      if (timestamp < since || timestamp >= until) { excluded.outside_window++; continue; }
      typeCounts[entry.type] = (typeCounts[entry.type] || 0) + 1;
      const day = new Date(timestamp).toISOString().slice(0, 10);
      if (entry.type === 'assistant') {
        const c = cell(lane, day);
        for (const b of blocksOf(entry)) {
          if (b.type === 'tool_use') {
            c.tool_calls++;
            const name = String(b.name || '');
            if (name.startsWith('mcp__memorymaster__')) {
              const tool = name.slice('mcp__memorymaster__'.length);
              if (['ingest_claim', 'ingest_rule', 'remember'].includes(tool)) c.ingest++;
              else { c.deliberate++; c.deliberate_by_tool[tool] = (c.deliberate_by_tool[tool] || 0) + 1; }
            } else if (name.startsWith('mcp__gitnexus__')) c.gitnexus++;
          } else if (b.type === 'text' && typeof b.text === 'string') {
            const ids = b.text.match(MM_ID) || [];
            for (const id of ids) {
              c.citations++;
              if (injected.has(id)) c.cited_of_injected++;
            }
          }
        }
      } else if (entry.type === 'user' || entry.type === 'attachment') {
        const raw = JSON.stringify(entry);
        if (raw.includes('[MemoryMaster recall]')) {
          const ids = raw.match(MM_ID) || [];
          const c = cell(lane, day);
          c.auto_recall++;
          c.injected_ids += ids.length;
          for (const id of ids) injected.add(id);
        }
      }
    }
  }
  const totals = {};
  for (const lane of Object.keys(agg)) {
    const t = newAgg();
    for (const day of Object.keys(agg[lane])) {
      const c = agg[lane][day];
      for (const k of SUM_KEYS) t[k] += c[k];
      for (const [tool, n] of Object.entries(c.deliberate_by_tool)) t.deliberate_by_tool[tool] = (t.deliberate_by_tool[tool] || 0) + n;
    }
    totals[lane] = t;
  }
  // Use the dated census's server-name inventory, never credentials or current config.
  const snapshot = profilesSnapshot ? JSON.parse(fs.readFileSync(profilesSnapshot, 'utf8')) : {};
  const mcp = snapshot.mcp_servers_per_profile || {};
  return { generated: new Date().toISOString(), window_days: 7,
    window_start_inclusive: new Date(since).toISOString(),
    window_end_exclusive: new Date(until).toISOString(), files_scanned: files.length,
    files_per_lane: perLane, script_sha256: sha, excluded_records: excluded, entry_type_counts: typeCounts,
    per_lane_per_day: agg, totals_7d: totals, mcp_servers_per_profile: mcp };
}

function renderAC4(result) {
  const esc = (text) => String(text).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const lanes = [...new Set(['wezbridge', 'memorymaster', 'infra', 'yolo26', 'futuramax',
    'wabot2', 'wisp', 'pedrito', ...Object.keys(result.totals_7d)])].sort();
  const total = newAgg();
  for (const counts of Object.values(result.totals_7d)) {
    for (const key of SUM_KEYS) total[key] += counts[key];
  }
  const columns = (counts) => ['auto_recall', 'deliberate', 'ingest', 'gitnexus']
    .map((key) => `<td>${counts[key]}</td>`).join('');
  const header = '<th>Automatic recall</th><th>Deliberate MCP</th><th>Ingest</th><th>GitNexus</th>';
  const daily = [];
  for (let day = Date.parse(result.window_start_inclusive);
    day < Date.parse(result.window_end_exclusive);) {
    daily.push(new Date(day).toISOString().slice(0, 10));
    day = Math.floor(day / 86400000) * 86400000 + 86400000;
  }
  return `<section>
<h2>AC4 · Usage by lane and UTC day; GitNexus recommendation</h2>
<p><b>Seven-day window:</b> ${esc(result.window_start_inclusive)} inclusive to
${esc(result.window_end_exclusive)} exclusive (168 hours). October 6 and 13 are partial UTC days.</p>
<p>Only records with a valid timestamp inside this window contribute to any count.
File modification time selects candidate files only; it never supplies an event date.
Excluded: ${result.excluded_records.outside_window} out-of-window records and
${result.excluded_records.missing_or_invalid_timestamp} missing/invalid timestamps.</p>
<table><tr><th>Lane</th>${header}</tr>
${lanes.map((lane) => `<tr><td>${esc(lane)}</td>${columns(result.totals_7d[lane] || newAgg())}</tr>`).join('\n')}
<tr><th>Total</th>${columns(total)}</tr></table>
<details><summary>Counts by lane and UTC day (zeros included)</summary>
<table><tr><th>Lane / UTC day</th>${header}</tr>
${lanes.flatMap((lane) => daily.map((day) => `<tr><td>${esc(lane)} / ${day}</td>${columns(result.per_lane_per_day[lane]?.[day] || newAgg())}</tr>`)).join('\n')}
</table></details>
<p class="note">Automatic recall counts user/attachment records carrying the recall marker;
deliberate MCP counts non-ingest MemoryMaster tool calls (including administrative calls);
ingest counts ingest_claim, ingest_rule and remember. Main and subagent JSONL files are included,
without resumed-session deduplication. Missing persisted hook payloads and shell/CLI calls are not
reconstructed. These are observed transcript events, not unique human requests or the AC1 citation metric.
Unmapped directories are combined as other:external; zero means no observed event in scanned files.</p>
<h3>MCP inventory per lane/profile</h3>
<p>Historical server-name snapshot from T-1344 (2026-10-13); configured profiles, not proof of live tool loading.</p>
<table><tr><th>Lane/profile</th><th>MCP servers</th></tr>
${[...new Set([...lanes.filter((lane) => lane !== 'other:external'), ...Object.keys(result.mcp_servers_per_profile)])].sort().map((lane) =>
    `<tr><td>${esc(lane)}</td><td>${esc(result.mcp_servers_per_profile[lane]?.join(', ') ||
      (result.mcp_servers_per_profile[lane] ? 'none' : 'unmeasured: no recorded profile'))}</td></tr>`).join('\n')}
</table>
<p><b>Recommendation:</b> retain MemoryMaster-only MCP profiles in product lanes. The recorded product
profiles omit GitNexus; observed GitNexus calls can come from earlier sessions and do not establish
a benefit from adding it to every lane. Keep it available to fleet/wezbridge and evaluate a bounded
product-lane trial before changing profiles. Call volume alone does not measure usefulness.</p>
<p class="note">Source: scripts/review_usage_census.cjs, tests/test_review_usage_census.cjs and
artifacts/2026-10-13-memorymaster-ac4-census.json. Recomputed from locally retained transcripts;
the historical source files may have grown since the original census. No claims, transcript text,
credentials or personal paths are included.</p>
</section>`;
}

async function main() {
  const { values } = parseArgs({ options: {
    root: { type: 'string', default: path.join(os.homedir(), '.claude', 'projects') },
    end: { type: 'string' }, output: { type: 'string' },
    'profiles-snapshot': { type: 'string' },
    artifact: { type: 'string' },
  } });
  const result = await census({ root: values.root, end: values.end,
    profilesSnapshot: values['profiles-snapshot'] });
  fs.writeFileSync(values.output, JSON.stringify(result, null, 2) + '\n');
  if (values.artifact) {
    const html = fs.readFileSync(values.artifact, 'utf8');
    const section = /<section>\s*<h2>AC4\b[\s\S]*?<\/section>/;
    if (!section.test(html)) throw new Error('AC4 section missing');
    fs.writeFileSync(values.artifact, html.replace(section, () => renderAC4(result)));
  }
  console.log(`Census written: ${result.window_start_inclusive} <= timestamp < ${result.window_end_exclusive}`);
}

module.exports = { census, renderAC4 };
if (require.main === module) main().catch(() => {
  console.error('Census failed; check input directory, end timestamp and output file');
  process.exitCode = 1;
});
