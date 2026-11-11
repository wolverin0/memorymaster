# Claims Lifecycle Rules

The claims DB is the source of truth. Every claim has a **status**, **tier**, **scope**, and **bitemporal fields**. Violating any of these invariants silently corrupts memory.

## Status transitions

Canonical statuses are defined in `memorymaster/models.py:CLAIM_STATUSES` — six values, no others are valid:

| Status | Meaning | Typically set by |
|--------|---------|------------------|
| `candidate` | Newly ingested, not yet validated | `ingest_claim` (default on insert) |
| `confirmed` | Validated — steward, dedup, or compaction promoted it | `llm_steward.py`, `jobs/compact_summaries.py` |
| `stale` | Decayed — no recent validation, past freshness window | `jobs/decay.py` |
| `superseded` | Replaced by a newer claim via explicit link | `auto_resolver.py`, `conflict_resolver.py` |
| `conflicted` | Contradicts another claim; needs human/steward arbitration | surfaced by `dashboard.py` + resolver jobs |
| `archived` | Terminal — retired by compaction, dedup, or explicit action | `jobs/compactor.py`, `jobs/dedup.py`, `llm_steward.py` |

- Transitions MUST go through `service.py` or the lifecycle helpers in `_storage_lifecycle.py` — never update `status` via direct SQL.
- Use `CLAIM_STATUSES` from `models.py` as the single source of truth when writing filters or validators. Adding a new status requires updating `models.py` + `schema.sql` CHECK constraint + `schema_postgres.sql`.
- When superseding, set both `supersedes_claim_id` on the new claim AND `replaced_by_claim_id` on the old one. Steward enforces the invariant; broken pairs break the wiki.
- `ingest_claim` MCP tool does NOT currently expose `supersedes_claim_id` as a parameter — the steward closes pairs asynchronously. If you need atomic supersession, go through `auto_resolver` or `conflict_resolver` paths rather than inventing a new ingest signature.

## Authors that never auto-promote (T-1354)

`memorymaster/core/promotion_policy.py` is the single policy point: claims whose `source_agent` is in `non_auto_promotable_sources()` (default `{"llm-stop-hook", "precompact-residual"}`, override with `MEMORYMASTER_NON_AUTO_PROMOTABLE_SOURCES`, empty disables) stay `candidate`. `transition_claim` raises `PromotionPolicyBlocked` for `-> confirmed` unless `event_payload["actor"]` is `operator` or `agent`; the validator, curation drain, Jev revalidation selection and the raw-SQL `llm_steward` confirm consult the same policy. A new automatic promoter must go through `transition_claim` (or call `auto_promotion_blocked`) and must not wrap the confirm in a broad `except` that downgrades the refusal to `conflicted`.

Structure policy (T-1591), same gate: a claim with a `predicate` but no `subject` is a shifted field, not a triple, and is never auto-promoted either (`structure_blocks_auto_promotion`). `subject`+`predicate` with no `object_value` is NOT malformed (dream-worker's normal shape). The loopback/docs-URL noise is fixed at the source instead: `govern/jobs/extractor.py:infer_structure` no longer stamps `server/ip_address` for loopback addresses nor `service/url` for loopback or generic docs URLs (`GENERIC_DOCS_DOMAINS`, currently `code.claude.com` only). Existing claims' triples were not rewritten.

## Corrections (T-2276)

- Declared: `ingest(..., supersedes=[<claim id or mm-xxxx>, ...])` (MCP `ingest_claim` / `checkpoint` item key `supersedes`, N->1) resolves each reference INSIDE the new claim's tenant and scope (other scope, unknown or already retired -> `ValueError`, nothing written) and files one `steward_proposal:superseded_candidate` per target (source `direct-ingest`, code `explicit_supersession_intent`). Ingest never applies it: the target stays `confirmed` until approval (`resolve_steward_proposal`) sets BOTH pointers. Recall demotes the target and hooks stop injecting it only once the replacement is itself `confirmed` (`pending_supersession_ids`, default `deliverable_only=True`; a separate long-lived MCP process caches the set up to 300 s): hooks serve confirmed claims only, so hiding the target earlier served neither value. Promotion gates (validator, Jev revalidation) pass `deliverable_only=False` and block the target whatever the replacement's state. The curation drain likewise leaves a proposal pending while its replacement is not `confirmed`, and rejects it as moot once the replacement is `archived`/`superseded`. A proposal whose replacement is not `confirmed` does not hold the target's slot against another replacement (`has_live_proposal`), so a correction that never becomes deliverable cannot block the next one (at most 3 live proposals per target). Known window: a long-lived process that cached the set before another process confirmed the replacement serves old and new together for up to 300 s. Dedup is against a LIVE proposal only (a rejected/approved one does not block a new one) and the ingest `supersession_report` / MCP `warnings` say when nothing was filed. Limits: 5 targets per call, 25 declared per agent per day. A target of another `source_agent` is `cross_identity`, so operator-only (`is_operator_only_proposal`): the drain applies only same-agent declared supersessions. `supersedes_claim_id` (single, cross-identity allowed) is unchanged.
- Fast path (ROADMAP 1.4): `MemoryService.validate_now(claim_id)` runs the cycle's own stages on ONE fresh candidate, in the cycle's order: deterministic format checks (a hard fail is `conflicted`), the near-duplicate decision (a match is left `candidate` for the cycle whatever this process's dedupe flags say: only candidates are deduped), the validator's gates and, if it is confirmed, the drain's own rule for the supersessions it declared (same-agent only). MCP `ingest_claim` and `checkpoint` call it after ingest, so a fact an agent states on purpose is deliverable at once; hooks, dreaming and direct `service.ingest` callers keep the steward cadence. It adds no promotion rule, never raises, and `MEMORYMASTER_INLINE_VALIDATION=0` turns it off. Like the cycle's validator it rewrites `confidence` to the validated score, so a caller-declared confidence no longer survives until the first cycle.
- N->1: `supersedes_claim_id` is single-valued, so the first predecessor gets the pointer pair; the others get status + `replaced_by_claim_id` and a `supersedes` claim link (`steward.retire_for_replacement`).
- Prose safety net: text that names an explicit `mm-xxxx` / `claim <id>` next to a correction cue (EN/ES, `govern/correction_text.py`) files the same proposal with source `prose-correction`. That source is in `steward.OPERATOR_ONLY_SOURCES`: the curation drain and every automation skip it. Reason (`promotion_policy`): only an `operator`/`agent` actor of `transition_claim` is an explicit decision, ingest cannot authenticate its author and a regex is an inference. No topic matching: prose naming no id matches nothing.
- Audit existing data with `scripts/find_prose_corrections.py` (read-only, ids only).

## Tiers (recall weight, orthogonal to status)

Tiers control recall ordering. Default stored in schema is `working` (see `schema.sql:26` and `_storage_schema.py:463`). The `recompute-tiers` CLI command reports counts for three buckets (see `cli_handlers_basic.py:455`):
- `core` — promoted (frequently accessed, high-confidence, load-bearing)
- `working` — default for new candidates and most claims
- `peripheral` — demoted (rarely accessed, low-confidence)

`recompute_tiers` runs on steward cycle; don't set tier manually unless you really mean it. Tier is NOT constrained by a schema CHECK — any string is accepted — so stay within the three canonical values above or you'll silently break CLI reporting.

## Scope conventions

Scope is a string used for auto-filter in queries. Canonical forms:
- `project:<slug>` — per-project (most common)
- `user` — user-level (workstyle, tools, cross-project preferences)
- `team:<name>` — team-shared
- `global` — system-wide facts

Do NOT invent new scope forms without updating `query_memory`'s scope filter logic. When in doubt, use `project:<slug>`.

## Bitemporal fields

Every claim has:
- `event_time` — when the fact OCCURRED in reality (optional, ISO-8601)
- `valid_from` — when the claim BECAME valid (defaults to `created_at`)
- `valid_until` — when the claim STOPPED being valid (omit if still current)

Use these to model "this was true until X" without superseding. Steward uses them for decay decisions.

## Convert relative dates to absolute

User messages often say "Thursday" or "next week." Always convert to ISO-8601 before storing in `event_time` or `valid_from`. Relative dates become meaningless 6 months later.

## When debugging a missing claim

1. `list_claims` unscoped — is it archived or superseded?
2. Check `visibility` — is it `sensitive` and hidden from query_memory?
3. Re-query with `include_stale=True, include_conflicted=True, include_candidates=True`
4. As last resort, check the raw `claims` table — but never edit rows directly.
