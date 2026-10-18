<!-- doc-head: T-1343 AC4 timestamp-window correction and verification -->
<!-- Covers: original census provenance, regeneration, counts and check limitations. -->
<!-- Read when: verifying the July-event exclusion repair for T-1343. -->
<!-- /doc-head -->
# T-1343 AC4 correction

The original generator and data are
`wezbridge/_intel/evidence/T-1344/census.cjs` and `census.json`, relative to
the Py Apps root. Original script SHA-256:
`4fa6795e463551fd4e4df10a6447fcc0c471a35e17b623fa4c21425a49d09d1b`.
They are external evidence, absent from this branch's base. The original HTML
was also untracked; its AC4 section was pending. This branch retains that HTML
with only AC4 replaced and brings the generator into `scripts/review_usage_census.cjs`.

The original selected recently modified files but counted every record in them,
including July. The correction checks each event timestamp before aggregation,
diagnostic counters and citation state. Missing/invalid timestamps are excluded;
file mtime never substitutes for a timestamp. UTC day buckets derive from the
parsed instant. Unknown directory labels are combined as `other:external`.

## Window and regeneration

The original census was generated at `2026-10-13T14:09:32.097Z`. That is the
explicit exclusive end of the corrected historical window; the inclusive start
is `2026-10-06T14:09:32.097Z` (168 hours). October 6 and 13 are partial UTC days.
The original script used `Date.now()` shortly before its generated timestamp;
its exact original start was not saved. This repair makes the boundary explicit.

From this checkout, with `PY_APPS_ROOT` set to the Py Apps root:

```powershell
node scripts/review_usage_census.cjs --end 2026-10-13T14:09:32.097Z --profiles-snapshot "$env:PY_APPS_ROOT/wezbridge/_intel/evidence/T-1344/census.json" --output artifacts/2026-10-13-memorymaster-ac4-census.json --artifact artifacts/2026-10-13-memorymaster-utility-review.html
```

The default input is the local Claude projects directory; `--root` can select
a retained copy. Raw transcripts and credentials are never written to output.
The profile snapshot supplies server names only, as recorded on October 13.
This is a historical profile inventory, not a live MCP loading probe.

Regenerated on October 18 from 2364 locally retained main/subagent JSONL files:

| Lane | Automatic recall | Deliberate MCP | Ingest | GitNexus |
|---|---:|---:|---:|---:|
| futuramax | 15 | 0 | 1 | 0 |
| memorymaster | 33 | 4 | 13 | 7 |
| wezbridge | 523 | 112 | 161 | 157 |
| wisp | 156 | 37 | 93 | 12 |
| other:external | 109 | 2 | 12 | 15 |
| **Total** | **836** | **155** | **280** | **191** |

Infra, pedrito, wabot2 and yolo26 have no counted events; the HTML includes
zero rows for them and every overlapping UTC day. Every JSON date bucket is
between October 6 and 13. There are 404210 out-of-window records and 149360
missing/invalid-timestamp records excluded from this scan. The data includes
the generator hash with LF line endings, matching Git's normalized source.

The retained source corpus may have grown since October 13. No resumed-session
deduplication or missing hook-payload reconstruction was added. Deliberate MCP
includes administrative tools, not just recall queries. These inherited counting
semantics remain separate from AC1's citation analysis. The artifact states these
limits and recommends retaining product lanes' recorded MemoryMaster-only
profiles pending a measured GitNexus trial.

## Verification

- Regression before the timestamp fix: direct `node tests/test_review_usage_census.cjs`
  failed with actual date buckets `2026-07-10` and `2026-10-12` rather than only
  `2026-10-12`.
- After the fix: **2 tests passed, 0 failed**. Coverage includes July tools,
  July automatic recall, both exact bounds, missing/invalid timestamps, UTC
  offset normalization, diagnostics and scrubbed report output.
- `node --test tests/test_review_usage_census.cjs` initially failed with sandbox
  `spawn EPERM`; direct execution runs the same `node:test` suite in process.
- No Python files changed. Broad `python -m ruff check scripts tests` found
  **81 existing findings** in unchanged Python files; they are outside this repair.
- The optional public-demo pytest check errored before test bodies: default
  temp directory access denied. A retry with the required heavy-slot wrapper and
  a configured writable temp root also failed with `WinError 5`. Neither attempt
  exercised product behavior; no service or authoritative database was changed.
- `python scripts/generate_release_truth.py --check`: **Release truth verified.**
- GitNexus check: `INDEX_MISSING`; client discovery exposed no GitNexus tools.
  Scoped live references found no existing callers of the external generator.
  Pre-commit scope review uses the staged source/artifact diff as fallback.
- Independent read-only review (`mm_security_reviewer`, configured
  `gpt-5.6-sol` / high): **PASS**, no blockers. Daily sums, HTML totals, window,
  exclusions, source hash and privacy checks agree. Only AC4 differs from the
  original HTML. The earlier locator worker could not start because its
  configured `gpt-5.4-mini` model was unsupported; the coordinator located the
  evidence through the T-1344 ledger entry without a replacement worker.
