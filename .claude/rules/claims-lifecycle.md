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

`memorymaster/core/promotion_policy.py` is the single policy point: claims whose `source_agent` is in `non_auto_promotable_sources()` (default `{"llm-stop-hook"}`, override with `MEMORYMASTER_NON_AUTO_PROMOTABLE_SOURCES`, empty disables) stay `candidate`. `transition_claim` raises `PromotionPolicyBlocked` for `-> confirmed` unless `event_payload["actor"]` is `operator` or `agent`; the validator, curation drain, Jev revalidation selection and the raw-SQL `llm_steward` confirm consult the same policy. A new automatic promoter must go through `transition_claim` (or call `auto_promotion_blocked`) and must not wrap the confirm in a broad `except` that downgrades the refusal to `conflicted`.

Structure policy (T-1591), same gate: a claim whose structured `object_value` is a loopback literal (`127.0.0.1`, `localhost`, `::1`, optional port or URL form) or that has a `predicate` without a `subject` is never auto-promoted either (`structure_blocks_auto_promotion`). Cause: `govern/jobs/extractor.py:infer_structure` stamps `server/ip_address/<first IPv4 in the text>` on prose that merely mentions an address. `subject`+`predicate` with no `object_value` is NOT malformed (dream-worker's normal shape). Private non-loopback IPs stay promotable.

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
