# MemoryMaster Native Dreaming V1
> Covers: quiet transcript capture, asynchronous LLM consolidation, governed candidate writes, rollout, measurement, and rollback.
> Key terms: Codex, Claude, Gemini 3.5 Flash, GLM 5.2, OpenCode account auth, capture ledger, shadow mode, candidate-first.
> Read this before enabling Dreaming hooks, scheduling the worker, changing provider models, or activating candidate writes.
> Default safety posture: disabled until explicitly installed; shadow processing before activation; never auto-confirms claims.
> Authority: the claims store remains authoritative; the auxiliary capture ledger is replay state, not a second memory database.
> Status: CURRENT implementation and operator contract for Dreaming V1.

## Intent

Dreaming turns eligible Codex and Claude conversations into a small number of durable, evidence-backed memory candidates. It is background consolidation, not a larger system prompt and not verbatim transcript storage. Recall continues to read governed MemoryMaster claims.

The design deliberately separates three jobs:

1. A quiet hook extracts user and assistant text, redacts locally, and appends an immutable capture envelope.
2. A bounded worker asks Gemini 3.5 Flash for evidence-linked candidates, then asks GLM 5.2 to compare them with current exact-scope claims.
3. The governed application layer may add or reinforce candidates, or create steward proposals for stale, conflict, or supersede decisions. It never confirms or destructively changes a claim directly.

## Data flow and authority

```text
Claude/Codex transcript
  -> local parser and redaction
  -> auxiliary replay ledger
  -> Gemini extraction
  -> GLM consolidation against exact-scope claims
  -> shadow report OR governed candidate/proposal application
  -> existing MemoryMaster lifecycle and steward
```

Only transcript message text is eligible. Reasoning, thinking blocks, tool calls, tool results, system messages, raw transcript paths, and raw session IDs are excluded. Stored sessions use hashes. Evidence quotes must be exact substrings of the already-sanitized message.

Project knowledge stays in its exact `project:<name>` scope. Stable user preferences, profile facts, and constraints may enter the separate `personal` lane. Code paths, commit hashes, project markers, and non-allowlisted claim types cannot be labeled personal.

## Safety properties

- Capture cursor advances only after the envelope is durably queued.
- Replay state is explicit: `captured`, `extracted`, `consolidated`, `applied`, `retryable`, or `quarantined`.
- A transactional expiring lease permits one worker at a time.
- Provider calls have finite timeouts, bounded execution, JSON validation, and no model fallback.
- Every candidate requires exact sanitized evidence and every candidate receives exactly one consolidation decision.
- Credentials in any candidate field, malformed numbers, unknown candidates, cross-scope targets, and malformed provider output fail closed.
- Applied decisions and proposal events use deterministic idempotency checks.
- Retention deletes only terminal applied or quarantined capture rows, never pending work.
- Status exposes counters and readiness only, never transcript content.

## Provider configuration

Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | none | Required by the extractor |
| `MEMORYMASTER_DREAM_EXTRACT_MODEL` | `gemini-3.5-flash` | Extraction model override |
| `MEMORYMASTER_DREAM_CONSOLIDATE_MODEL` | `zai-coding-plan/glm-5.2` | OpenCode provider/model override |
| `MEMORYMASTER_OPENCODE_COMMAND` | discovered from `PATH` | Optional explicit OpenCode executable |
| `MEMORYMASTER_CAPTURE_STATE_DB` | platform default | Auxiliary ledger location |
| `MEMORYMASTER_DREAM_MAX_SEMANTIC_ATTEMPTS` | `2` | Quarantine bound for repeatedly malformed extraction evidence |

Gemini reads its key at call time. GLM does not require a separate MemoryMaster API key: the worker invokes `opencode run --pure` with the existing `Z.AI Coding Plan` account session and model `zai-coding-plan/glm-5.2`. The prompt is supplied over stdin, all OpenCode tools, configured GitNexus/Playwright MCPs, plugins, Claude compatibility, and external instructions are disabled for the call, and output is accepted only from JSON events that pass the Dreaming decision schema. The worker deletes the OpenCode session it created after parsing the result, including schema-rejection paths, so hourly runs do not accumulate a second transcript archive. OpenCode credentials remain owned by OpenCode and are never read, copied, logged, or persisted by MemoryMaster.

Verify account readiness without exposing credentials:

```powershell
opencode auth list
opencode models | Select-String 'zai-coding-plan/glm-5.2'
```

The scheduled task must run as the same Windows user that authenticated OpenCode. Missing CLI/account/model availability produces an actionable, retryable failure; it never silently switches providers.

## Installation and modes

Normal MemoryMaster setup does not register Dreaming. Explicit setup installs the central hook, preserves unrelated client hooks, removes the superseded Claude immediate session-end distiller, and schedules one hourly worker on Windows:

```powershell
memorymaster-setup --enable-dream --yes
```

That command is shadow mode. The worker may capture, extract, consolidate, and report, but it cannot write claims or steward proposals.

Read-only verification:

```powershell
memorymaster-setup --verify-only --enable-dream --json
memorymaster --json dream-status
```

One manual shadow pass:

```powershell
memorymaster --db memorymaster.db dream-run
```

Candidate application is a separate explicit activation and should happen only after the evaluation gate passes:

```powershell
memorymaster-setup --enable-dream --dream-apply-candidates --yes
```

No setup or worker command in this document authorizes live cleanup, compaction, migration, archival, redaction, or backlog mutation.

## Usefulness evaluation and activation gate

Label real shadow decisions as JSONL and run:

```powershell
python scripts/evaluate_dreaming.py path\to\dreaming-labels.jsonl
```

Each row has `record_id`, boolean `should_emit`, `emitted`, and `structured_valid`. Emitted rows also have boolean `evidence_exact`, expected/actual scope, expected/actual action, and optionally boolean `human_accept`.

Activation requires all of these:

| Gate | Threshold |
|---|---:|
| Labeled decisions | at least 50 |
| Human-reviewed emitted decisions | at least 20 |
| Evidence precision | 95% |
| Ephemeral rejection | 90% |
| Scope isolation | 100% |
| Consolidation action accuracy | 85% |
| Structured-output yield | 95% |
| Human acceptance | 80% |

An invalid or incomplete label blocks activation. Synthetic unit fixtures prove the evaluator contract but do not count as activation evidence.

## Operations

`dream-status` reports pending states, run/provider counters, structured yield, 429s, hook error count, scheduler freshness, and warnings. The first exhausted Gemini 429 opens a batch circuit so later captures wait for the next run instead of amplifying throttling. Repeated semantic evidence failures quarantine after two attempts for review rather than looping forever. Sustained GLM concurrency is intentionally one because the reused Z.AI account has shown throttling above two concurrent callers elsewhere. OpenCode runs in an isolated non-repository directory with tools and inherited MCP startup denied, so it cannot modify source or absorb project instructions. A recurring Windows task under the authenticated user is used instead of shell-detached processes.

The first real rollout should remain shadow-only for at least 48 hours. Review provider yield, retry/quarantine counts, scope mistakes, ephemeral candidates, evidence accuracy, estimated cost, and a human-labeled sample before considering activation.

## Rollback

1. Disable or delete the `MemoryMaster-Dreaming` scheduled task.
2. Remove only entries containing `memorymaster-dream-capture.py` from Claude/Codex hook configuration.
3. Leave the auxiliary ledger intact until pending rows are reviewed; it is not used by recall.
4. If candidate application was enabled, stop the task first. Existing candidates remain governed and can be reviewed through normal steward workflows.
5. Do not delete or rewrite the claims database as part of Dreaming rollback.

## V1 boundaries

- Sources are Codex and Claude transcript formats only.
- No ChatGPT-memory import, system-prompt dump, paid-provider smoke test, or automatic production activation is part of implementation.
- Provider availability, 48-hour shadow evidence, and 50-decision human labeling are runtime rollout gates, not conditions that code tests can honestly manufacture.
- Updating the repository `DOCS-MAP.md` is required when this branch is integrated. It was intentionally not synthesized here because the main checkout currently owns an uncommitted map.
