<!-- doc-head: Workflow Intelligence v1 implementation and shadow-rollout contract -->
# Workflow Intelligence
# Covers: local trajectory census, deterministic analysis, reports, candidate governance, and receipt hooks.
# Key terms: workflow sidecar, human root session, verification tier, correction, shadow receipt.
# Read when: auditing coding-agent history, reviewing candidates, or evaluating hook activation.
# Safety: rebuildable analytics never become authoritative memory or activate instructions automatically.
<!-- /doc-head -->

Workflow Intelligence analyzes retained Claude Code, Codex, and Wezbridge
history without making raw transcripts part of MemoryMaster's governed claim
store. It is analytics plus hardening—not reinforcement learning, model
training, autonomous policy promotion, or a second memory authority.

## Storage and trust boundary

The default database is
`~/.memorymaster/workflow-intelligence.db`; override it with
`MEMORYMASTER_WORKFLOW_DB` or the CLI's global `--workflow-db` option. It is a
rebuildable WAL-mode SQLite sidecar. `memorymaster.db` remains authoritative
for claims, evidence, lifecycle decisions, approved skills, and recall.

The sidecar stores normalized metadata, bounded redacted excerpts, byte
offsets, hashes, and deterministic signals. It does not store complete tool
inputs or outputs. Public reports never expose local source paths. Excerpts are
limited to 400 characters and pass through shared secret redaction plus
absolute-path and private-IP redaction.

Deleting the sidecar loses analytics and human review labels, but cannot lose
or change governed memory. Re-running `workflow scan` rebuilds transcript-derived
state; review labels should therefore be exported before an intentional rebuild.

## Source census and trajectory parsing

`workflow scan` discovers the formats that actually exist under the current
user's Claude and Codex directories. It recognizes:

- Claude project JSONL, history, session metadata, and explicit `subagents/`;
- Codex active and archived rollouts, history, and session index JSONL;
- direct Wezbridge `events.jsonl` and `a2a-results.jsonl` beneath the workspace;
- skill-outcome files and hashes of global/project instruction and hook config.

Metadata is indexed for every supported source. Deep parsing is explicit:

```powershell
memorymaster workflow scan
memorymaster workflow scan --deep human
memorymaster workflow scan --deep selected --session <external-session-id>
```

`--deep human` parses human/mixed root sessions and excludes subagent-only
traffic from the human-correction denominator. `--deep selected` is the only
v1 route for deep-parsing a named subagent or automation session. There is no
`--deep all` mode. Parsers stream JSONL, ignore incomplete trailing records,
record byte offsets, and detect source replacement through size/prefix metadata.

## Deterministic analysis

The default pipeline makes no provider call. It records:

- reads/research before the first mutation;
- repeated failed command families and retry loops;
- user corrections grouped into bounded themes;
- completion claims separately from observed verification;
- Wezbridge request/result/ack closure metadata;
- instruction/config hashes for policy-drift comparison.

Verification is an ordered evidence vocabulary:

1. `none`
2. `syntax_static`
3. `unit`
4. `integration_build`
5. `runtime_api`
6. `browser_visual`
7. `deployed_identity`
8. `natural_external_acceptance`

Completion states are `implemented`, `locally_verified`, `runtime_verified`,
`deployed`, `externally_accepted`, `partial`, `blocked`, or `unknown`. An exit
code, commit, final answer, or user silence is not acceptance.

## Optional classification

`memorymaster workflow classify --limit 50` is the only command that invokes
the configured MemoryMaster LLM provider. Each call receives at most 4,000
characters of redacted, structured context. Transcript text is explicitly
untrusted. Output must match a strict task/outcome JSON vocabulary; provider,
model, and prompt hashes are stored. Classification is never authoritative for
success or recurrence.

## Reports, candidates, and reviews

```powershell
memorymaster workflow inspect <session-id>
memorymaster workflow report --scope project:memorymaster
memorymaster workflow candidates --status proposed
memorymaster workflow review <candidate-id> --decision accept_pattern
memorymaster workflow proposal <candidate-id> --output proposal.json
```

Reports default to
`~/.memorymaster/reports/workflow-intelligence/<UTC-run-id>/report.{html,json}`.
HTML is self-contained and has no remote assets. Reports separate human,
subagent, and automation sessions and expose unknowns rather than inventing
outcomes.

Candidate grouping is deterministic. A project candidate needs three distinct
human root sessions. A user/global candidate additionally needs support from
two projects. `accept_pattern`, `reject_noise`, `watch`, and `relabel` are
review labels only. Exported proposals are inert JSON. Existing `remember`,
`skill-propose`, and `skill-review` surfaces remain the governed promotion
boundary.

## Rule and skill recurrence hardening

Migration `0024_rule_observation_lineage` adds `rule_observations` to both
SQLite and Postgres schemas. A root session is stored only as a SHA-256 hash.
Repeated mining in the same provider/root tuple increments `event_count`; it
does not create independent support. Subagent and automation observations are
diagnostic only.

`rule_stats.correction_count` remains legacy activity telemetry. New governed
skill candidates require three independent human roots; a user/global candidate
also requires two projects. Confirmed skills are grandfathered. Historical
rows are not silently backfilled as authoritative evidence.

## Completion receipt hook

The provider-neutral hook is shipped but unregistered. Its mode is controlled
by `MEMORYMASTER_WORKFLOW_RECEIPTS=off|shadow|advisory`; default is `off`.

Preview setup without writes:

```powershell
memorymaster-setup --dry-run --workflow-receipts shadow --json
```

Explicit shadow installation:

```powershell
memorymaster-setup --workflow-receipts shadow `
  --workflow-db "$HOME/.memorymaster/workflow-intelligence.db"
```

The hook reads a bounded current-turn transcript tail, performs no LLM call,
writes no claim or repository data, never blocks completion, and stores only
hashed/content-free receipts. A read-only turn cannot warn unless an independent
mutation was observed.

`memorymaster workflow receipt-review` labels shadow warnings for precision
measurement. `memorymaster workflow shadow-status` requires all of:

- at least 14 days and 100 eligible receipts;
- at least 20 Claude and 20 Codex receipts;
- a manual warning sample;
- at least 90% measured precision;
- zero read-only false positives.

Passing this gate does not activate anything. Advisory installation still
requires a separate explicit setup command, and the installer refuses advisory
mode until the recorded shadow gate passes. Blocking mode is not part of v1.

## Deliberate non-goals

V1 does not add a scheduler, web dashboard, MCP server, transcript embeddings,
DuckDB, an observability SaaS, automatic instruction edits, automatic skill
activation, model-weight training, or reinforcement learning. Build those only
after local reports and the shadow gate demonstrate a concrete need.

