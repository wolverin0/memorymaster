<!-- doc-head: T-1433 measured post-trim similarity versus index and on-demand reading -->
<!-- Covers: AC1 noise labels, AC3 paired offline replay, character costs and recommendation. -->
<!-- Evidence: 60 real prompt events; 57 assessable; no database opened or runtime changed. -->
<!-- /doc-head -->
# T-1433: measured comparison after the recall-hook trim

Measured on 2026-10-18 against source revision `e1e2129f1a144404d86784996830f85a4c790007`.
**AC1 PASS (measurement): 83/113 injected claims irrelevant, 73.45%, on 57
assessable prompt events. AC3 PASS (bounded offline comparison): index plus
title-guided reads uses 696.58 characters/prompt versus 1,825.07 for similarity
injection, recovering 28/30 relevant candidate claims (93.33%).** This closes
the missing measurement, not a deployment or a claim of corpus-wide recall.

Recommend an opt-in pilot of **index plus on-demand reading**: it cuts measured
text by 61.83% including reads, and irrelevant bodies from 83 to 15. Improve
title quality before considering a default change: two useful bodies were
missed. The index itself still exposes irrelevant titles; its noise fraction
must not be reported as zero. Keep the current production configuration for
this evidence-only task. The existing A/B ending on October 28 compares other
variants and is not the evidence for this result.

## Sources, time boundary and sampling

Only existing local files were read. No network requests, service recall calls,
embedding calls, hook executions, SQLite connections, migrations, access-record
writes or authoritative database reads/writes were used. No installed hooks,
session state, logs or configuration were changed. Scratch analysis wrote only
temporary code and ordinal labels in this worktree; no source text was copied
into the committed evidence.

Source pointers are relative to the checkout or the user's home, avoiding
personal paths:

- `memorymaster/recall/hook_policy.py`: two-claim limit, session deduplication
  and trivial-prompt skip. T-1433 merged on October 15 at 14:59:21 UTC; the
  window starts at 15:00 UTC, after that merge and the October 13 volume trim.
- `memorymaster/recall/context_hook.py`, `_recall_inner`: telemetry records
  `query_fp`, `injected_ids`, `injected`, `chars` and `ts` after rendering.
  `_finish_recall` applies the hook cap and session deduplication.
- `memorymaster/recall/dense_recall.py`, `query_fingerprint`, `record` and
  `read_events`: the existing home-relative
  `.memorymaster/metrics/recall-dense.jsonl` stream; fingerprints are the first
  12 hex characters of SHA-256 of the exact query. The stream has no prompt text.
- Home-relative `.claude/projects/*/*.jsonl`: actual UserPromptSubmit
  `hook_additional_context` attachments containing the delivered recall block.
  Parsing reuses `is_real_prompt`, `prompt_text`, `parse_time`, `recall_texts`
  and bounded persisted-output resolution from
  `scripts/review_recall_citations.py`. Citation rate was not used as relevance.

The fixed window is **2026-10-15 15:00:00 through 2026-10-18 06:32:08 UTC**.
There are 366 telemetry events in this interval. One malformed line in the
whole telemetry file was skipped, rather than repaired or counted as a prompt.
Recent top-level transcript files were scanned, without subagent directories
or sidechain rows. Compaction clears the current-prompt association. A recall
attachment is paired with its preceding real prompt in that transcript.

There were 222 recall blocks in the window. Exclusions before sampling: eight
explicit notification/auto-save/fleet-tick prompts, 51 without a matching
query fingerprint, and 68 without a unique size-and-time match. A match requires
equal fingerprint, `chars == len(delivered_context_without_hook_marker)`, and
timestamps within 20 seconds. Parsed bullet count must equal `injected`, with
one or two bullets: thus the measured events themselves demonstrate the trim.
This leaves **95 eligible, UUID-deduplicated prompt events**. Unmatched events
were excluded, never assigned a zero cost or an irrelevant label.

Sort eligible events ascending by
`SHA256("T-1433-20261018:" + prompt_uuid)` and take the first 60, before looking
at relevance. This samples actual injection traffic, including repeated
scheduled tasks and worker-start/inbox instructions, not just human-authored
conversation. The 60 events have 48 distinct query fingerprints and 119
injected claim occurrences. Repeated prompts in separate turns count because
their injections consume context again; resumed copies with the same UUID do
not. The earliest sampled attachment is October 15 at 15:02:42.502 UTC; the
latest is October 18 at 05:26:25.567 UTC.

Three sampled events need missing conversational/image context to determine
relevance. Their six claims are `U`, not assumed irrelevant. The primary paired
analysis uses the remaining **57 events, 45 distinct queries, 113 claim
occurrences**, distributed by UTC day: October 15: 14; October 16: 16;
October 17: 18; October 18: nine. The complete 60-row ledger below retains the
excluded events and their costs.

For local reconstruction, the sampled private manifest consists, in sample
order, of dictionaries with keys `uuid`, `ts`, `query_fp`, `ids` and `chars`;
`ids` is telemetry `injected_ids`. Canonical serialization is
`json.dumps(manifest, sort_keys=True, separators=(",", ":"))`, UTF-8.
Its SHA-256 is
`7c63e19fc8794e1744e6441d0ac6aa3fa816cd2b6af22aa57af37c6f11028707`.
The manifest and source text remain private; only this checksum is published.

## Labels and the two variants

The judge/reader was **GPT-6 in this Codex session**, using direct review of
the locally retrieved material; no separate judge API or model process ran.
The backend-specific model version/effort was not exposed. This is one LLM
reader and one labeling pass, not human ground truth or independent annotator
agreement. Source views masked addresses, URLs and paths; this artifact
publishes only paraphrased categories, ordinal positions, labels and counts.

Relevance rubric, applied to each full claim against the current prompt:

- `R`: a directly usable constraint, factual distinction or operational caveat
  for the requested action. A historical caveat can qualify when its concrete
  command/component is the one being operated; this does not verify that the
  historical incident remains unresolved.
- `I`: merely shares a project, word or broad theme; concerns a different
  component/action; or supplies an old task/incident with no demonstrated
  bearing on this request. Generic lessons are not automatically relevant.
- `U`: the current prompt depends on missing conversation or an unavailable
  image. Exclude it from the primary relevance denominators, not from the
  disclosed sample inventory. Other conversation history was not reconstructed.

**A, current similarity injection:** replay the exact delivered claim bullets
and header from the matched attachment. Use telemetry `chars`, cross-checked
against that attachment. No retrieval was rerun, so actual thresholds,
authorization filtering, session deduplication and rendered truncation remain
as observed. Labels correspond to every injected claim occurrence.

**B, compact index plus on-demand reading:** use the same observed candidates,
order and short IDs. Replace each body with a title produced mechanically by
collapsing whitespace and taking its first 72 Unicode characters. Titles were
not rewritten using relevance labels. The exact serialization is:

```python
title = re.sub(r"\s+", " ", body).strip()[:72]
index = "[Memory index; read a claim body by id only when needed]\n"
index += "\n".join(f"- [{cid}] {title}" for cid, title in candidates)
```

The reader saw the prompt and these titles first, chose zero, one or two IDs,
and froze all 60 choices before inspecting full bodies for the relevance pass.
Those frozen choices are the `Read` column below. This is an actual local
title-guided selection replay, not an oracle that reads only gold-positive
claims: it includes 15 false-positive reads. Requested bodies were fetched
from the already delivered attachment in memory, by selected position/ID,
without calling a service or opening a database. Read serialization is
`"\n\n".join(f"[{cid}]\n{body}" for selected bodies)`; an empty selection is
zero characters. The full previously rendered body is returned, with no
additional summarization.

Both variants use Python `len` (Unicode characters, not bytes or tokens).
Counts include each variant's displayed header/IDs/separators; the common
`[MemoryMaster recall]` transport marker, JSON envelopes and tool schemas are
excluded. B's total includes its index on every prompt **plus all requested
body text**, including mistaken reads. Read-call latency, tool-request syntax,
model reasoning tokens and downstream answer quality are not measured.

## Results on the same 57 assessable prompt events

| Measure | A: similarity bodies | B: index + title-guided reads |
|---|---:|---:|
| Prompt events / labeled candidate occurrences | 57 / 113 | 57 / 113 |
| Automatically exposed candidate items | 113 bodies | 113 ID/title entries |
| Irrelevant automatic items / all automatic items | 83/113 = **73.45%** | 83/113 = **73.45%** (titles of irrelevant claims) |
| Relevant claims available in initial output | 30/30 = **100%** | 30/30 = **100%**, IDs present in index |
| Full bodies consumed | 113 | 43, requested on 31/57 prompts |
| Irrelevant consumed bodies / all consumed bodies | 83/113 = **73.45%** | 15/43 = **34.88%** |
| Relevant bodies consumed / labeled relevant candidates | 30/30 = **100%** | 28/30 = **93.33%** |
| Prompts with a relevant body / prompts with relevant candidates | 19/19 = **100%** | 18/19 = **94.74%** |
| Automatic text, total / mean characters per prompt | 104,029 / **1,825.07** | 12,813 / **224.79** |
| Additional requested text, total / mean characters per prompt | 0 / 0 | 26,892 / **471.79** |
| Combined text, total / mean characters per prompt | 104,029 / **1,825.07** | 39,705 / **696.58** |
| Irrelevant full bodies per prompt | **1.456** | **0.263** |

The index reduces automatic text by 87.68%; counting reads, reduction is
**61.83%**. Irrelevant body occurrences decrease **81.93%**, from 83 to 15.
There are no unsolicited bodies in B, but its automatic-body irrelevance
fraction is **undefined (0/0), not 0%**. The 73.45% title-noise rate above
prevents treating compression as a relevance improvement in retrieval.

Relevant-ID coverage is **conditional on the observed candidate pool**, not
recall over all authoritative claims. A's 100% and B's index 100% are expected
because the candidate IDs are held constant. B's 93.33% measures the additional
loss from selecting reads using titles. Telemetry does not identify all
non-injected candidates or all relevant claims; global retrieval recall cannot
be inferred from these logs. This experiment evaluates changing delivery, not
replacing the similarity retriever with a global static catalog.

The two missed relevant bodies are S24 position 2 (operational review advice
hidden behind a generic historical-review prefix) and S55 position 1 (a plan
exception hidden behind configuration-change metadata). There were no title
retries or post-label corrections to the frozen read choices.

Across all 60 events, A exposes 108,802 characters (1,813.37/prompt); B's index
is 13,491 (224.85/prompt), and index plus reads is 40,383 (673.05/prompt).
If all six `U` claims were relevant, A's irrelevant fraction would be
83/119 = 69.75%; if all were irrelevant, 89/119 = 74.79%. They were not silently
assigned to either extreme. These are sample descriptions, with repeated
traffic and one judge; no independent-observation confidence interval or
fleet-wide quality claim is asserted.

## Anonymized labeling and cost ledger

`Labels` follows delivered candidate order; `Read` lists the frozen positions
requested by the title-only reader. `A`, `Index` and `Read chars` are integer
character counts. `U` rows are omitted only from the primary 57-event totals.
Categories describe task types; they are not prompt or claim excerpts.

| Sample | UTC day | Paraphrased category | Labels | Read | A chars | Index chars | Read chars |
|---|---|---|---|---|---:|---:|---:|
| S01 | 2026-10-16 | Context-dependent follow-up | UU | - | 1822 | 226 | 0 |
| S02 | 2026-10-17 | Network observation task | II | - | 2333 | 226 | 0 |
| S03 | 2026-10-17 | Single-file write probe | I | - | 318 | 141 | 0 |
| S04 | 2026-10-17 | Single-command automation audit | II | 1 | 1009 | 226 | 454 |
| S05 | 2026-10-16 | Radio equipment identification | II | - | 1600 | 226 | 0 |
| S06 | 2026-10-16 | Worker delegation request | RR | 1,2 | 1922 | 226 | 1843 |
| S07 | 2026-10-15 | Plugin model configuration question | II | 2 | 2004 | 226 | 895 |
| S08 | 2026-10-17 | Start assigned remediation tasks | II | - | 1197 | 226 | 0 |
| S09 | 2026-10-16 | Single-command automation audit | II | 1 | 1009 | 226 | 454 |
| S10 | 2026-10-15 | Plugin analysis completeness | II | - | 1757 | 226 | 0 |
| S11 | 2026-10-18 | Complete pending workflow | II | - | 2935 | 226 | 0 |
| S12 | 2026-10-16 | Notify another owner | II | 2 | 1985 | 228 | 924 |
| S13 | 2026-10-15 | Process orchestration inbox | RI | 1 | 1528 | 226 | 383 |
| S14 | 2026-10-16 | Scheduled reconciliation review | RR | 1,2 | 1794 | 226 | 1715 |
| S15 | 2026-10-16 | Modernization scope sequencing | II | - | 2734 | 226 | 0 |
| S16 | 2026-10-17 | Process orchestration inbox | RR | 1,2 | 1114 | 226 | 1035 |
| S17 | 2026-10-16 | Radio panel status clarification | II | - | 2209 | 226 | 0 |
| S18 | 2026-10-17 | Start assigned remediation tasks | II | - | 2806 | 226 | 0 |
| S19 | 2026-10-15 | Project-scoped hook policy | IR | 2 | 803 | 228 | 100 |
| S20 | 2026-10-15 | Daily project operations review | RR | 1,2 | 726 | 226 | 647 |
| S21 | 2026-10-18 | Independent fleet oversight | II | 1,2 | 1362 | 226 | 1283 |
| S22 | 2026-10-17 | Scheduled reconciliation review | RR | 1,2 | 1794 | 226 | 1715 |
| S23 | 2026-10-17 | Stop duplicate workflow | II | 1 | 1666 | 226 | 877 |
| S24 | 2026-10-18 | Review and release coordination | RR | 1 | 1227 | 226 | 500 |
| S25 | 2026-10-18 | Restore interrupted session | II | - | 1152 | 226 | 0 |
| S26 | 2026-10-15 | Worker startup instructions | IR | 2 | 1404 | 226 | 650 |
| S27 | 2026-10-16 | Scheduled reconciliation review | RR | 1,2 | 1794 | 226 | 1715 |
| S28 | 2026-10-17 | Single-command smoke check | II | - | 2433 | 226 | 0 |
| S29 | 2026-10-18 | Close duplicate application | II | - | 3157 | 226 | 0 |
| S30 | 2026-10-15 | Documentation-only health review | II | - | 5530 | 226 | 0 |
| S31 | 2026-10-17 | Single-command fleet audit | II | 2 | 1009 | 226 | 454 |
| S32 | 2026-10-15 | Worker startup instructions | II | 1 | 1303 | 228 | 549 |
| S33 | 2026-10-16 | Scheduled reconciliation review | RR | 1,2 | 1794 | 226 | 1715 |
| S34 | 2026-10-17 | Start test remediation task | II | 1 | 1590 | 228 | 549 |
| S35 | 2026-10-17 | Daily project operations review | RR | 1,2 | 726 | 226 | 647 |
| S36 | 2026-10-16 | Image-dependent follow-up | UU | - | 1514 | 226 | 0 |
| S37 | 2026-10-18 | Fleet audit coverage | RR | 1,2 | 1494 | 226 | 1415 |
| S38 | 2026-10-16 | Service-restoration communication | RR | 1,2 | 1169 | 226 | 1090 |
| S39 | 2026-10-17 | Single-command smoke check | II | - | 2433 | 226 | 0 |
| S40 | 2026-10-17 | Process orchestration inbox | II | - | 2992 | 226 | 0 |
| S41 | 2026-10-18 | Close completed work accurately | IR | 2 | 1895 | 226 | 658 |
| S42 | 2026-10-17 | Single-command smoke check | II | - | 2433 | 226 | 0 |
| S43 | 2026-10-16 | Single-command fleet audit | II | 2 | 1009 | 226 | 454 |
| S44 | 2026-10-18 | Single-command smoke check | II | - | 2433 | 226 | 0 |
| S45 | 2026-10-15 | Live-code classification concern | II | - | 2271 | 226 | 0 |
| S46 | 2026-10-16 | Authentication coordination and inbox | II | 2 | 1130 | 228 | 500 |
| S47 | 2026-10-15 | Worker startup instructions | RI | 1,2 | 1280 | 228 | 1201 |
| S48 | 2026-10-18 | Remote-host workflow complexity | II | - | 2837 | 226 | 0 |
| S49 | 2026-10-16 | Single-command smoke check | II | - | 2433 | 226 | 0 |
| S50 | 2026-10-15 | Context-dependent status question | UU | - | 1437 | 226 | 0 |
| S51 | 2026-10-15 | Worker startup instructions | II | 1 | 1303 | 228 | 549 |
| S52 | 2026-10-16 | Image-processing approach choice | II | - | 2366 | 226 | 0 |
| S53 | 2026-10-16 | Resume guarded workflow | II | - | 3134 | 226 | 0 |
| S54 | 2026-10-17 | Investigate worker throughput | II | - | 828 | 228 | 0 |
| S55 | 2026-10-15 | Plan completeness and hook exception | RI | - | 1398 | 226 | 0 |
| S56 | 2026-10-16 | Chat observation task | IR | 2 | 1362 | 226 | 567 |
| S57 | 2026-10-15 | Single-command smoke check | II | - | 2433 | 226 | 0 |
| S58 | 2026-10-17 | Network observation task | II | - | 2333 | 226 | 0 |
| S59 | 2026-10-15 | Worker startup instructions | RI | 1 | 1404 | 226 | 650 |
| S60 | 2026-10-17 | Remediation handoff to owner | II | 2 | 1935 | 226 | 704 |

## Verification and acceptance boundary

The paired sums and label/read intersections were freshly recomputed locally
from the attachments and frozen ordinal decisions, then separately summed
from this public table. Each included attachment has one or two parsed claims,
matching its telemetry count and character length. No unobserved injections
were synthesized. Useful categories include inbox-consumption semantics,
operational isolation, concrete reconciliation caveats and accurate completion
reporting. Noise categories include command-name similarity, unrelated terminal
history, old cross-project task inventories and generic lessons for a different
action.

Source checks: the branch freshness script reports zero commits behind the
recorded main baseline; checkout began clean. The ledger's three acceptance
criteria were read using `ledger.cjs show T-1433`. No ledger mutation was made.
The worktree has no GitNexus index and its MCP tools are not exposed in this
session after tool discovery; graph change detection is unavailable. Native
Git scope/whitespace checks cover this documentation-only change; no source
symbols, runtime behavior or activation flags changed. Application tests are
not evidence for these traffic labels and were not rerun for this report.

AC1 passes its requirement to measure post-trim irrelevant injections; the
observed noise level remains high. AC3 passes its requirement for a measured
comparison and recommendation with explicit limitations. Neither pass grants
production activation, establishes corpus-wide recall, validates the safety of
historical advice, or supplies independent human quality acceptance. A pilot
should test better titles, real read-tool overhead and answer quality on
additional labeled traffic before changing the default.
