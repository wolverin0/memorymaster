'use strict';
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { census, renderAC4 } = require('../scripts/review_usage_census.cjs');

test('a recently modified transcript excludes July events from the seven-day census', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ac4-census-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const lane = path.join(root, 'G---OneDrive-OneDrive-Desktop-Py-Apps-memorymaster');
  fs.mkdirSync(lane);
  const event = (timestamp) => ({ type: 'assistant', timestamp, message: { content: [
    { type: 'tool_use', name: 'mcp__memorymaster__query_memory' },
    { type: 'tool_use', name: 'mcp__memorymaster__ingest_claim' },
    { type: 'tool_use', name: 'mcp__gitnexus__query' },
  ] } });
  const events = [event('2026-07-10T12:00:00Z'), event('2026-10-12T12:00:00Z')];
  fs.writeFileSync(path.join(lane, 'resumed.jsonl'), events.map(JSON.stringify).join('\n'));
  const result = await census({ root, end: '2026-10-13T14:09:32.097Z' });
  assert.deepEqual(Object.keys(result.per_lane_per_day.memorymaster), ['2026-10-12']);
  assert.equal(result.totals_7d.memorymaster.deliberate, 1);
  assert.equal(result.totals_7d.memorymaster.ingest, 1);
  assert.equal(result.totals_7d.memorymaster.gitnexus, 1);
});

test('timestamp bounds apply to recalls, tools and diagnostics; dates use UTC', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ac4-census-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const lane = path.join(root, 'private-directory-name');
  fs.mkdirSync(lane);
  const recall = (timestamp) => ({ type: 'user', timestamp,
    message: { content: '[MemoryMaster recall] [mm-1234]' } });
  const events = [
    recall('2026-07-10T12:00:00Z'),
    recall('2026-10-06T14:09:32.096Z'), // One millisecond before the window.
    recall('2026-10-06T14:09:32.097Z'), // Inclusive start.
    recall('2026-10-13T14:09:32.096Z'), // Last included millisecond.
    recall('2026-10-13T14:09:32.097Z'), // Exclusive end.
    recall('2026-10-14T00:00:00Z'),
    recall(undefined), recall('invalid'),
    recall('2026-10-12T23:30:00-03:00'), // October 13 UTC.
  ];
  fs.writeFileSync(path.join(lane, 'resumed.jsonl'), events.map(JSON.stringify).join('\n'));
  const result = await census({ root, end: '2026-10-13T14:09:32.097Z' });
  assert.equal(result.window_start_inclusive, '2026-10-06T14:09:32.097Z');
  assert.equal(result.window_end_exclusive, '2026-10-13T14:09:32.097Z');
  assert.equal(result.per_lane_per_day['other:external']['2026-10-13'].auto_recall, 2);
  assert.equal(result.totals_7d['other:external'].auto_recall, 3);
  assert.equal(result.entry_type_counts.user, 3);
  assert.deepEqual(result.excluded_records, { missing_or_invalid_timestamp: 2, outside_window: 4 });
  assert.ok(!JSON.stringify(result).includes('private-directory-name'));
  const html = renderAC4(result);
  assert.ok(html.includes('2026-10-06T14:09:32.097Z'));
  assert.ok(html.includes('2026-10-13T14:09:32.097Z'));
  assert.ok(!html.includes('2026-07-10'));
  assert.ok(!html.includes('private-directory-name'));
});
