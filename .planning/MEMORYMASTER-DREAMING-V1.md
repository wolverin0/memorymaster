# MemoryMaster Native Dreaming V1
> Covers: quiet transcript capture, asynchronous LLM consolidation, governed candidate writes, rollout, measurement, and rollback.
> Key terms: Codex, Claude, OpenCode OAuth, GPT-5.6 Terra, exact evidence spans, capture ledger, candidate-first.
> Read this before enabling Dreaming hooks, scheduling the worker, changing provider models, or activating candidate writes.
> Default safety posture: disabled until explicitly installed; shadow processing before activation; never auto-confirms claims.
> Authority: the claims store remains authoritative; the auxiliary capture ledger is replay state, not a second memory database.
> Status: CURRENT implementation and stabilization contract; replacement 24-hour observation passed.

## Intent

Dreaming turns eligible Codex and Claude conversations into a small number of durable, evidence-backed memory candidates. It is background consolidation, not a larger system prompt and not verbatim transcript storage. Recall continues to read governed MemoryMaster claims.

The design deliberately separates three jobs:

1. A quiet hook extracts user and assistant text, redacts locally, and appends an immutable capture envelope.
2. A bounded worker asks the configured Gemini or OpenCode extractor for evidence-linked candidates, then asks the configured OpenCode model to compare them with current exact-scope claims.
3. The governed application layer may add or reinforce candidates, or create steward proposals for stale, conflict, or supersede decisions. It never confirms or destructively changes a claim directly.

## Data flow and authority

```text
Claude/Codex transcript
  -> local parser and redaction
  -> auxiliary replay ledger
  -> configured Gemini or OpenCode extraction
  -> OpenCode consolidation against exact-scope claims
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
- Extraction and consolidation quotas are counted by provider/model so two stages using one OAuth provider do not consume each other's stage budget.
- Budget exhaustion defers captured or extracted work without incrementing attempts, creating retry errors, or failing the scheduled task.
- Consolidation batches contain at most five candidate IDs and keep one capture together when possible, reducing identifier transcription failures.
- OpenCode extraction selects deterministic sanitized evidence-span IDs; MemoryMaster resolves the exact source message and quote, and unknown IDs fail closed.
- Project-specific candidates mislabeled personal may route only back to their capture's project scope; ambiguous personal facts remain rejected.
- Every candidate requires exact sanitized evidence and every candidate receives exactly one consolidation decision.
- Credentials in any candidate field, malformed numbers, unknown candidates, cross-scope targets, and malformed provider output fail closed.
- Applied decisions and proposal events use deterministic idempotency checks.
- Retention deletes only terminal applied or quarantined capture rows, never pending work.
- Status exposes counters and readiness only, never transcript content.

## Provider configuration

Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `MEMORYMASTER_DREAM_EXTRACT_PROVIDER` | `gemini` | Select `gemini` or authenticated `opencode` extraction |
| `GEMINI_API_KEY` | none | Required only by the Gemini extractor |
| `MEMORYMASTER_DREAM_EXTRACT_MODEL` | provider default | `gemini-3.5-flash` or `openai/gpt-5.4-mini` |
| `MEMORYMASTER_DREAM_EXTRACT_VARIANT` | `medium` | OpenCode extraction reasoning-effort variant |
| `MEMORYMASTER_DREAM_CONSOLIDATE_MODEL` | `zai-coding-plan/glm-5.2` | OpenCode provider/model override |
| `MEMORYMASTER_DREAM_CONSOLIDATE_VARIANT` | none | Optional OpenCode reasoning-effort variant |
| `MEMORYMASTER_DREAM_MAX_CONSOLIDATE_CANDIDATES` | `5` | Maximum candidate IDs in one consolidation call |
| `MEMORYMASTER_OPENCODE_AUTH_MODE` | automatic | Set `oauth` to exclude provider API-key variables from child processes |
| `MEMORYMASTER_OPENCODE_COMMAND` | discovered from `PATH` | Optional explicit OpenCode executable |
| `MEMORYMASTER_CAPTURE_STATE_DB` | platform default | Auxiliary ledger location |
| `MEMORYMASTER_DREAM_MAX_SEMANTIC_ATTEMPTS` | `2` | Quarantine bound for repeatedly malformed extraction evidence |

Gemini reads its key at call time. OpenCode extraction and consolidation do not
require separate MemoryMaster API keys: the worker invokes `opencode run
--pure` with the selected provider/model and OpenCode's authenticated account
session. Setting `MEMORYMASTER_OPENCODE_AUTH_MODE=oauth` removes the selected
provider's API-key variable from the child process, making OAuth use explicit.
The optional variants are passed as `--variant`. The prompt is supplied over
stdin; tools, configured GitNexus/Playwright MCPs, Claude compatibility, and
external plugins or instructions are disabled. OpenCode's internal
authentication plugin remains enabled because it owns the OAuth session.
Output is accepted only from JSON events that pass the Dreaming schemas. The
worker deletes every OpenCode session it creates after parsing the result,
including schema-rejection paths, so hourly runs do not accumulate a second
transcript archive. OpenCode credentials remain owned by OpenCode and are never
read, copied, logged, or persisted by MemoryMaster.

The local vNext stabilization uses ChatGPT OAuth for both stages:
`openai/gpt-5.6-terra` at medium effort extracts typed evidence-linked
candidates, while `openai/gpt-5.6-luna` at low effort performs the harder
lifecycle comparison. GPT-5.4 Mini was removed from the local extraction
configuration after live runs reproduced exit failures, malformed JSON, and
low exact-evidence yield. Terra remains separate from Luna so model-specific
stage budgets cannot consume one another. These are local activation choices;
the portable extractor default remains Gemini.

Verify account readiness without exposing credentials:

```powershell
opencode auth list
opencode models openai | Select-String 'openai/gpt-5.6-luna'
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

The initial real rollout was required to remain shadow-only while provider yield,
retry/quarantine counts, scope mistakes, ephemeral candidates, evidence accuracy,
estimated cost, and human-reviewed samples were evaluated. The completed
activation and replacement 24-hour observation evidence is recorded in
`.planning/audits/2026-07-27-vnext-governed-capture/audit-delta.md`; future
rollouts must establish an equivalent bounded observation instead of assuming
that earlier evidence covers a new provider or configuration.

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
