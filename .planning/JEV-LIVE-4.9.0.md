<!-- doc-head: implementation contract for MemoryMaster 4.9.0 — live TypeSafe Jev decisions with an RL-grade decision ledger, plus the 2026-09-23 review remediation -->
Covers: decisions package API, egress redaction, transport, decision ledger schema, exploration/propensity, breaker/budget, 8 surfaces (S1-S8), outcome joiners, metrics, tests, track ownership.
Key terms: surface, question version, decision_id, propensity, exploration arm, was_exposed, held, egress_blocked, breaker.
Read when: implementing or reviewing any 4.9.0 track. Subordinate to ROADMAP.md ("Live Jev decisions" section); design rationale in artifacts/2026-09-23-plan-jev-y-curacion.html.
Findings referenced as F-xx live in .planning/audits/2026-09-23-weekly-claude-review/REPORT.md (+ addendum F-20/F-21).
Base: branch release/4.9.0-jev-live = 6909841 (installed 4.8.9) + 55a5871 (F-04 ProviderOutputError).
<!-- /doc-head -->

# 4.9.0 implementation contract

Operator decision (2026-09-23): turn Jev on **live in production, no smoke-test phase**, log every
decision and outcome for calibration / off-policy evaluation / RL, and cure F-01..F-21.
Non-negotiables that make "live without smoke test" safe:

1. **SQLite stays authoritative.** Jev only chooses among IDs already authorized by code; scope,
   tenant, visibility, sensitivity and status are checked in code before and after.
2. **Every live action is reversible or a proposal.** Jev never deletes, archives, degrades,
   merges or supersedes by itself. Allowed live actions: order/inject recall items, choose k,
   suggest a skill, route a Dreaming candidate to `held` (kept, releasable), re-confirm a stale
   claim (positive lifecycle event), write a proposal/edge for the steward.
3. **Deterministic fallback always.** Timeout, HTTP 401/403/429/529/5xx, malformed answer,
   egress blocked, breaker open, budget exhausted, flag off, missing key -> legacy behaviour,
   logged with `transport_outcome` / `fallback_reason`. A late answer never changes an action.
4. **No private data leaves unredacted.** Egress goes through one redactor; if a credential-grade
   finding survives redaction the request is not sent (`egress_blocked`).
5. **Everything is logged, append-only.** Including fallbacks, blocked egress and shadow mode.

## Package `memorymaster/decisions/`

| Module | Contract |
|---|---|
| `config.py` | `DecisionConfig.from_env()`. Global `MEMORYMASTER_JEV_MODE` (`off`/`shadow`/`live`, default `off` in code), per-surface override `MEMORYMASTER_JEV_<SURFACE>` (surface ids below, upper-case). `MEMORYMASTER_JEV_DAILY_USD_CAP` (default 2.0), `MEMORYMASTER_JEV_RPM_CAP` (default 600), `MEMORYMASTER_JEV_HOOK_DEADLINE_MS` (default 900), `MEMORYMASTER_JEV_BATCH_DEADLINE_MS` (default 8000), `MEMORYMASTER_JEV_EXPLORE_<SURFACE>` (defaults: recall 0.10, ingest 0.05, others 0.0), `MEMORYMASTER_DECISIONS_DB` (default `~/.memorymaster/decisions.db`), `MEMORYMASTER_DECISIONS_STATE_RETENTION_DAYS` (default 180). Truthiness parsing identical everywhere (`1/true/yes/on`). |
| `credentials.py` | API key: `TYPESAFE_API_KEY` env, then Windows `HKCU\Environment` (so long-lived processes started before the key existed still find it). Never logged, never in argv, never in exceptions (wrap and re-raise without the value). |
| `egress.py` | `prepare_egress(text) -> EgressText(text, counts: dict, blocked: bool, reason: str|None)`. Composes `core.security.redact_text` / `scan_text_for_findings` with: private IPv4 + IPv6 ULA/link-local, email addresses, home paths (Windows, POSIX, `/c/Users/...`, UNC `\\wsl$\...\home\<u>`, `~user`), internal hostnames (`.local .lan .internal .corp .home`), URLs with userinfo, high-entropy tokens >=20 chars of any case near secret keywords or with Shannon entropy >= 4.0. ISO dates/times, public URLs and ordinary identifiers must NOT be redacted (review F-10 false positives). Must pass every case in `evidence/review-A/r5_query_egress.py` and `evidence/review-B/r3_redact.py` / `r4_falsepos.py` (encode them as tests). Length cap per field (default 1,200 chars). |
| `transport.py` | In-process stdlib `http.client.HTTPSConnection` + `ssl.create_default_context()` (system trust store, certifi fallback; no proxy env, no redirects; ruling R6 replaced the original httpx client, whose cold construction broke the hook deadline), endpoint pinned `https://api.typesafe.ai/v1/systemone`, model pinned `jev-1.13.0`, response cap 128 KiB, no subprocess (fixes F-15 class; review F-10 latency). Returns `TransportResult(status, body|None, latency_ms, attempt_count, outcome)`. Hooks: zero retries within the deadline. Batch jobs: exponential backoff on 429/529 (max 3). Validates: `model` present and recorded as served, every question id answered, probabilities finite in [0,1], Choice/Score distributions sum to 1±0.01, noul in [0,1]; otherwise `malformed`. Records `usage.input_tokens/output_tokens`; cost = input_tokens × 0.042e-6 (price table versioned in code). |
| `questions.py` | Registry of `QuestionSpec(id, version, primitive, instructions, criteria/options)`; `sha256` over canonical JSON; builders per surface. Questions are direct, literal contracts (jev-1.13 jaggedness): no dates/arithmetic/counting, no double negation, one narrow judgment each, candidate text only in its own question, never many memories in one state unless each lives in its own question. Changing wording = new version. |
| `ledger.py` | SQLite sidecar (WAL, busy_timeout 15 s, `PRAGMA user_version` schema v1). Tables below. Writes never raise into callers (count failures in `ledger_write_failures`, log once). Append-only; outcomes never overwrite decisions. Prune job nulls `state_redacted` after retention days, keeps hashes/features. Concurrency-safe across processes (hooks from several panes). |
| `policy.py` | Exploration: deterministic per decision via `sha256(decision_id + surface)` → uniform u; if u < rate take the surface's predefined safe alternative. Always log `available_actions`, `action_propensities_json` (probability the logging policy gave each action) and `chosen_propensity`. Breaker: per surface, open for 15 min after 5 consecutive failures or >30 % fallback over the last 10 min (min 10 decisions) → surface behaves as `shadow` and logs `fallback_reason=breaker_open`. Budget: daily USD from ledger sum + RPM token bucket → `budget_exhausted`. Thresholds read from `question_versions.threshold_json` (seeded by code defaults). |
| `engine.py` | `decide(surface, *, state, questions, items, legacy_action, choose, context) -> Decision`. Orchestrates mode/breaker/budget → egress → transport (with deadline) → validation → `choose(answers)` → exploration → ledger write → returns `Decision(decision_id, action, items, mode, fallback_reason)`. In `shadow`, the legacy action is returned but Jev's action is logged. In `off`, nothing is sent and a minimal row is logged (`mode=off`) only if `MEMORYMASTER_DECISIONS_LOG_OFF=1`. |
| `outcomes.py` | Joiners (idempotent, watermark-based, never mutate authoritative data): (a) `record_turn_usage(session_key, transcript_turn)` — for items exposed in that session's recent S2/S8 decisions: `used_in_turn` if the claim `human_id` or a distinctive 8-gram of its text appears in the assistant text or tool-call inputs of the turn; (b) `tail_lifecycle(db)` — events for claim ids in `decision_items` since watermark → outcomes (`steward_confirmed`, `archived`, `superseded`, `stale`, `conflicted`, `revalidated`), `label_source` from event actor (see F-21); (c) `record_skill_invocation`; (d) `record_held_release`. |
| `metrics.py` | Aggregations used by dashboard + operational review: per surface volume, live %, fallback by reason, latency p50/p95/p99, tokens, cost/day vs cap, breaker opens, egress blocked, decision/probability distributions per question version, agreement with legacy, exposure→use rates (Jev vs legacy vs explored), reliability bins + ECE + Brier where outcomes exist, weekly PSI drift. |
| `export.py` | Training/OPE export (JSONL): one row per item with question version, probabilities, action, propensity, exposure, outcomes, label_source; time-split flag. |

### Ledger schema v1

```
decisions(decision_id TEXT PK, ts TEXT, surface TEXT, mode TEXT, policy_version TEXT,
  question_set_id TEXT, question_sha256 TEXT, primitive_summary TEXT,
  model_requested TEXT, model_served TEXT, backend TEXT, transport_version TEXT, sdk_version TEXT,
  code_revision TEXT, state_schema_version INT, state_sha256 TEXT, state_redacted TEXT,
  egress_bytes INT, redaction_counts_json TEXT, transport_outcome TEXT, fallback_reason TEXT,
  latency_ms INT, attempt_count INT, tokens_in INT, tokens_out INT, cost_usd REAL,
  legacy_action TEXT, jev_action TEXT, available_actions_json TEXT, action_taken TEXT,
  exploration_arm TEXT, action_propensities_json TEXT, chosen_propensity REAL, randomization_id TEXT,
  thresholds_json TEXT, baseline_features_json TEXT, session_key TEXT, scope TEXT, tenant TEXT)
decision_items(decision_id TEXT, item_ref TEXT, item_kind TEXT, question_id TEXT, question_version INT,
  answer TEXT, probabilities_json TEXT, confidence REAL, rank_legacy INT, rank_final INT,
  exposed INT, delivered INT, PRIMARY KEY(decision_id, item_ref, question_id))
outcomes(outcome_id INTEGER PK, decision_id TEXT, item_ref TEXT, kind TEXT, value REAL,
  was_exposed INT, outcome_window TEXT, reward_version TEXT, label_source TEXT,
  observed_at TEXT, lag_s INT, details_json TEXT, UNIQUE(decision_id, item_ref, kind, observed_at))
question_versions(question_id TEXT, version INT, sha256 TEXT, primitive TEXT, text_json TEXT,
  threshold_json TEXT, created_at TEXT, PRIMARY KEY(question_id, version))
watermarks(name TEXT PK, value TEXT, updated_at TEXT)
```

## Surfaces

| Id | Where | Questions (primitive) | Live action | Exploration (safe alt) | Outcome |
|---|---|---|---|---|---|
| `REVALIDATE` (S1) | new `govern/jobs/revalidation.py`, steward cycle before `scheduled_archive`, plus CLI `memorymaster jev-revalidate --limit N` for the one-time backlog | per stale claim, one request: `lifecycle.still_valid` (noul), `lifecycle.durable` (noul: not tied to a transient state), `lifecycle.useful_future` (noul). State = claim text, subject/predicate/object, claim_type, scope label; ages/dates computed in code are passed only as named buckets | still_valid ≥ t and useful ≥ t → `stale→confirmed` via lifecycle helpers, event `validator`, details prefix `jev_revalidation:` + decision_id; confidence set to validator promote score. Otherwise stays stale; record `no_longer_useful` judgment when both < low threshold | none | exposure/use in 30 days, later stale/superseded/contradicted, operator correction |
| `RECALL` (S2) | recall hook `context_hook` ranking step + MCP `recall`/`query_for_context` | ONE request per prompt: state = redacted query (+ project label); per candidate (top 20 authorized) four questions carrying that candidate's redacted text: `recall.relevant` (score, 4 levels), `recall.usable_evidence` (noul), `recall.contradicts_request` (noul), `recall.instruction_like` (noul) | order by relevant score; k adaptive 2–5 (include while usable_evidence ≥ t); contradiction/instruction-like items are labelled in the injected block, not hidden | 10 %: sample order of top-5 by Plackett-Luce over relevance (propensity logged) | `used_in_turn` (Stop detector), requery, correction; log `exposed` AND `delivered` (after delivery suppression) |
| `INGEST` (S3) | Dreaming after extraction, before consolidation | per candidate, one request, eight nouls mirroring `source_review.CHECKS` (evidence, chronology, modality, scope, specificity, privacy, usefulness, novelty) with the candidate + its evidence quote as state | all ≥ thresholds → proceeds to consolidation; else candidate marked `held` in `extraction_json` (skipped by consolidation, releasable via CLI `memorymaster jev-release-held`) | 5 % of held → admitted anyway (arm `explore_admit`) | steward/consolidator verdict for admitted, held_later_confirmed via release, later use |
| `DEDUP` (S4) | steward cycle, `candidate_dedupe` candidate pairs (FTS top 5) | per pair: `memory.same_fact` (score 3 levels: different / maybe / same), `memory.contradicts` (noul), `memory.supersedes` (noul, asked in two paraphrases), `memory.same_scope` (noul) | write a claim edge / steward proposal tagged `source: jev` with evidence; **no status change**; `curation_drain` must NOT auto-approve `source: jev` proposals (F-21 actor field) | none | operator/steward resolution with actor, reappearing conflicts |
| `SKILLS` (S5) | `knowledge/jev_selector.py` ported onto engine/transport (no subprocess) | existing two-round design + cookbook gate | suggest 0–1 skill | none | skill invoked / wrong load |
| `HINTS` (S6) | `config_templates/hooks/memorymaster-classify.py` | one request, 7 nouls (decision, constraint, bug_root_cause, environment, reference, architecture, preference) over the redacted prompt | hints shown = labels ≥ threshold; regex is the fallback | none | an `ingest_claim` of that type followed in the session and survived steward |
| `ROUTE` (S7) | `recall/query_classifier.py` (only where auto-classify is used) | Choice over the 7 existing types + `unknown` | chosen type (unknown → keyword rules) | none | latency, result used |
| `SESSION` (S8) | `config_templates/hooks/memorymaster-session-start.py` | per candidate (30 most recent **confirmed** in scope; stale/candidate excluded — current bug), `session.relevant_to_project` (score) with project/cwd label as state | top 5 injected by score | none | used_in_turn during the session |

## Remediation owned by tracks (tests first: every review repro becomes a regression test, red on 6909841, green after)

- **Track D (Dreaming):** F-04 token accounting on failed calls; F-05 counter resets only on stage success, survives deferral/transient, absolute per-capture error cap → quarantine; F-06 packed consolidation batches, per-capture isolation only after a packed call fails; F-07 lease renewal per batch + ownership check before each ledger write. Files: `dreaming/*`, `core/antigravity_client.py` (timeouts only), tests.
- **Track R (recall/lifecycle/stores):** F-13 `status_in` in `list_claims_page` (SQLite + Postgres); F-14 lexical stream pages until 60 authorized; F-11 normalized session key hashed with tenant; F-18 `PRAGMA optimize` after migrations; F-20 `scheduled_archive` filters in SQL AND requires a recorded S1 `no_longer_useful` decision for the claim (query the decisions ledger via a small interface; absent ledger/decision → do not archive); F-21 explicit `actor` (`operator`/`automation`) in steward proposal resolution, curation_drain passes `automation`, `source: jev` proposals excluded from auto-approval; F-12 operational review supports several canaries; F-17 import sentence-transformers only when the effective embedding provider needs it; F-03 compiled profile can source supports from confirmed claims and Dreaming applications when verbatim is empty + operational review freshness check (max support age) and the `hook.log` date fix. Files: `recall/*`, `stores/*`, `govern/*`, `profile/*`, `operations/*`, `surfaces/mcp_server.py` (F-17 only), `core/hook_log.py`, tests.
- **Track C (decisions core):** the whole `memorymaster/decisions/` package + tests + `docs/jev-decisions.md`. Fixes the F-15/F-16/F-10 class centrally (transport/egress). No surface wiring.
- Surfaces S1–S8, dashboard tab, operational-review `jev_decisions` check, outcome wiring into Stop hook, CLI commands and scripts (`scripts/jev_calibration_report.py`, `scripts/jev_ope.py`, `scripts/jev_export.py`, review-queue selection) come in the second wave on top of C.

## Engineering rules for every track

- Work only in your assigned worktree/branch; commit atomically with conventional messages ending with the attribution trailer given in the session; never push/merge.
- Unset every `MEMORYMASTER_*` and `TYPESAFE_*` variable before running tests; temp DBs only; never open `memorymaster.db` or anything under `~/.memorymaster`; no real provider/TypeSafe calls in tests (mock transport or local HTTP server).
- Use `--basetemp` under `%TEMP%\<track>` on the system drive (T: is saturated).
- Run targeted tests + Ruff on changed files; regenerate release truth (`python scripts/generate_release_truth.py`, then `--check`).
- Keep SQLite/Postgres parity where a store API changes (Postgres fail-closed is acceptable for SQLite-only features, with a test).

## Rulings recorded during execution (orchestrator, 2026-09-23; conservative defaults under the operator's "decide and proceed" order)

- **R1** Configuration/auth provider failures (missing key, invalid/expired key, 401, 403, model not found, retired provider, missing CLI, Gemini `FAILED_PRECONDITION`) are provider-wide: never charged to captures; the run stops.
- **R2** Poison consolidation batch: isolated after two consecutive breaker trips, ordered last; a capture that alone trips it three runs in a row is charged one error per run up to quarantine.
- **R3** Home-path redaction covers WSL `/mnt/<d>/Users/<u>`, lowercase `/users/<u>` and `~user/`; `dream_bridge` (local files only) accepts a bare `~word` in prose.
- **R4** A compiled-profile support whose claim text no longer matches its stored hash is retracted.
- **R5** Pre-F-21 overrides without a payload actor are labelled `unknown_actor` and never count as ground truth (one shared `UNTRUSTED_LABEL_SOURCES`).
- **R6** The decisions transport uses stdlib `http.client` + `ssl.create_default_context()` (system trust store, certifi fallback on verification failure). Measured cold hook process: httpx 1.0–1.25 s to answer vs stdlib 0.43–0.69 s; the 900 ms hook deadline is only reachable without httpx.
- **R7** Held Dreaming candidates keep their capture for at most 30 days (`MEMORYMASTER_DREAM_HELD_RETAIN_DAYS`); quarantined captures are never exempt; expiry is logged as `held_expired`.
- **R8** S3 questions distinguish the operator's own preferences from third-party personal data (privacy v2, usefulness v2, personal-scope variant of `ingest.scope`), so personal memory is not held by construction.
- **B1** Live recall never loses authorized memory: private/sensitive rows are not sent to Jev and keep their legacy position in the output.
