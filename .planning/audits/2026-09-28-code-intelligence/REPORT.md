<!-- doc-head: installed cross-client code navigation and bounded retrieval evidence -->
<!-- Covers: GitNexus refresh, Serena, three client smokes, frozen tasks and limitations. -->
<!-- Read when: assessing navigation acceptance or interpreting context savings. -->
<!-- /doc-head -->
# Shared code navigation — 2026-09-28

Operator authorized implementation and testing for AGY, Claude Code and Codex.
Result: GitNexus and a five-tool Serena navigation profile are configured in
all three clients. Native and Orca-managed Codex homes are both covered.
This is agent tooling; MemoryMaster's claim authority and feature activation
settings were not changed. No production schema or governed-recall code changed.

## Installed and exercised

| Surface | Evidence | Verdict |
|---|---|---|
| GitNexus | Installed 1.4.7; main index refreshed from September 6 to source HEAD `5d87e02f4d301b881872f76fb85edc0f6891f204`; 1,268 files, 18,782 nodes, 47,977 edges, 14,603 embeddings. Previous 12,262 embeddings cached/preserved. | PASS for refresh; graph completeness not established |
| Serena | Installed `serena-agent` 1.7.0; LSP backend, per-session stdio, current-project detection, five navigation tools, no memory/onboarding tools, no dashboard/browser launch. | PASS |
| Codex | Fresh ephemeral client recorded actual `gitnexus/context` and `serena/find_symbol` calls in JSONL and returned the expected symbol. | PASS for actual calls |
| Claude Code | Fresh no-persistence client returned both MCP calls successful; no permission denials; selected alias `sonnet`, receipt reports `claude-sonnet-5`. | PASS for client-reported calls; final JSON does not retain a full tool trace |
| AGY | Same test conversation resumed after initial MCP permission denial; final scoped `permissions.allow` grants permit both calls without `--dangerously-skip-permissions`. | PASS for client-reported calls; final JSON does not retain a full tool trace |
| Live-source isolation | Disposable projects A/B: old-project symbol excluded after switch, current symbol returned, dirty source update visible, overflow suppresses body, return to MemoryMaster successful. | PASS, six checks |
| References | Serena finds `context_bundle.py`, `cli_handlers_skills.py`, and `mcp_server.py` references to `recall_skills`, independently present in scoped source search. GitNexus reported an empty incoming set. | Serena PASS; GitNexus completeness FAIL for this symbol |
| Freshness guard | Four disposable-repository tests cover clean index, dirty/new source, sibling checkout, stale HEAD, missing/invalid metadata, clone exclusion. | PASS |
| Graphify | Installed `graphifyy` 0.4.0, metadata points to `safishamsi/graphify`. Bounded query succeeds but returns unrelated cloned upstream files from an old July 23 graph. | NOT selected for default navigation |

The first CLI results repeated Serena's zero-based range `680–744`; the actual
editor range is `681–745`. Installed tool descriptions now explicitly require
adding one, not only the initialization prompt. Follow-up Claude and Codex
smokes both returned **681–745**, with both MCP calls successful. One intervening
Codex attempt reported Serena unavailable; the next attempt explicitly used
deferred-tool discovery and recorded both actual calls. This failed attempt
remains retained; tool availability must not be inferred from the first visible
tool list. The final AGY follow-up also returned **681–745**, with both calls
reported successful and no denied actions under scoped permissions. All three
clients therefore passed the final editor-line check.

## Frozen retrieval experiment

Independent source inspection produced `cohort.json`: 20 questions across eight
domains, including five exact-symbol questions. Per-file git blob and SHA-256
hashes were verified before querying. Source HEAD is recorded above. Cohort SHA:
`62c0b9dc8285a2d17310ab02cb6dc1791c123907405ec6dfad9087a0011f785f`.

| Measurement | Result | Interpretation |
|---|---:|---|
| Fixed OR-keyword/file-frequency baseline, expected file in first five | 7/20 | Deterministic heuristic, not an agent baseline |
| GitNexus natural/exact query, expected file in first five | 9/20 | Standalone navigation is insufficient |
| Union of the two file sets | 12/20 | Complementary hits, not a measured adaptive agent policy |
| Serena exact-symbol lookup without the gold file path | 5/5 | Small exact-name discovery sample |
| Serena known-path/name body delivery within 6,000 characters | 19/20 | Gold location supplied: NOT a discovery metric |
| Delivered bodies checked against live source | 19/19 | First-line symbol indentation normalized; remaining lines exact |
| Oversized body | 1/20 | 6,367-character response refused at the 6,000-character limit; not counted as success |
| Payload on the 19 comparable successful lookups | 30,477 vs 383,880 characters | 92.06% less than independently reading the known whole file for each lookup |

The last row is **conditional tool-payload reduction**, not billed-token savings,
not unique-corpus reduction, and not an end-to-end coding result. It excludes
the overflow case and does not account for caching/reuse of an already-read file.
No claim of 92% cheaper sessions is warranted.

Local call P50/P95: lexical baseline 47/62 ms; GitNexus 210.5/390 ms; Serena
known-target lookup 70.5/141 ms. These are measured calls from one run, not a
hardware-independent guarantee. Separate first Serena launch/lookup was 39.64 s;
GitNexus indexing took 136.4 s. Startup/indexing are not included in warm-call
latency. The trial did not compare 20 full agent tasks across all three clients.

## Client token/cost observations

These initial successful smokes are connectivity evidence, **not paired savings evidence**.
Initial prompts and existing instructions/tool schemas dominate small tasks.

- Claude's successful receipt reports 6 input, 65,089 cache-creation input,
  124,877 cache-read input and 605 output tokens, with list-price cost $0.2913934.
  That reported estimate is not an invoice or the total cost of this task.
- Codex's corrected smoke reports 108,939 input tokens, of which 80,896 cached,
  and 415 output tokens. Requested model: `gpt-6-luna`. Cost unknown.
- AGY's resumed receipt reports 66,900 input, 2,648 output and 73,608 cache-read
  tokens. Its counters/duration cover the resumed conversation and are not
  directly comparable with the other clients. Served-model attestation and
  cost are absent from this receipt: unknown.
- Worker authoring/review and failed trials are additional usage. No aggregate
  task cost or agent-level savings percentage has been established.
  Subsequent permission/discovery/line-format validation also adds usage; these
  initial figures do not represent the total of the final repeated checks.

## Repairs, preservation and reproducibility

- First Orca worker failed readiness and was released using its exact receipt.
  The replacement used an explicit MemoryMaster path; inherited `current`
  resolved incorrectly in the original Orca dispatch. No task ran there.
- Initial Windows `.cmd` smoke launch mishandled the multiline prompt. Native
  Claude executable / Node Codex entrypoint repaired argv delivery. Original
  transcripts are retained and excluded from acceptance and token comparisons.
- Initial evaluator matched Windows paths as raw text and counted an overflow
  message containing a symbol name. Offline JSON/path/body validation corrected
  0/5 to 5/5 exact discovery and 20/20 to 19/20 delivered bodies. Initial metrics
  remain retained; no paid rerun was used to hide these defects.
- Indexer-appended AGENTS/CLAUDE blocks were captured, then only those appendages
  removed after confirming the original contents remained a byte-identical prefix.
- Existing dirty ingestion/ledger/docs and upstream clones were preserved.
  Configuration backups and manifests remain private under
  `~/.local/state/code-intelligence-20260928/`.
- AGY's test reused one conversation after its permission denial. Unlike the
  Claude/Codex print runs, AGY did not expose an ephemeral flag; its test session
  was retained. No broad session cleanup was attempted.
- The first successful AGY diagnostic used `--dangerously-skip-permissions` for
  that process. Independent review required narrower evidence. Added exact
  read-navigation allow-rules in AGY's own `antigravity-cli/settings.json` and
  Claude settings, then verified AGY without the bypass. Existing rules remain
  intact; no wildcard or editing/shell grant was added.

Raw local evidence and runnable probe/evaluator scripts are under
`artifacts/code-intelligence/20260928/`. Run `benchmark.py`, then `summarize.py`
to reproduce the final normalized metrics. `evidence-hashes.json` binds the
frozen evidence; checked-in `benchmark-summary.json` contains the final counts.
Use `python scripts/check_code_navigation.py` for subsequent checkout checks.
Its WARN on the existing dirty checkout is intentional, not a failed reindex.

Validation: initial focused tests **6 passed** (four guard tests plus two public
demo tests); after narrowing status enumeration, guard tests **4 passed**.
Ruff passed for both changed Python files. Generated release truth was regenerated
and verified after adding four tests. `INDEPENDENT-REVIEW.md` independently
re-derived every metric, passed four guard tests, and identified the line-format
and AGY permission findings addressed above. Its snapshot precedes those final
repairs; it is preserved as written. No feature-success watermark follows from
these tool receipts.
