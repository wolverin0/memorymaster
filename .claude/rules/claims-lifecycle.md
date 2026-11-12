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
| `archived` | Terminal (one exception below) — retired by compaction, dedup, or explicit action | `jobs/compactor.py`, `jobs/dedup.py`, `llm_steward.py` |

- `archived` is terminal except the documented same-author re-ingest revival to `candidate` (`MemoryService._revive_archived_dedup_match`; blocked for claims archived by `forget`). `reconcile_integrity` accepts exactly that event (`core.lifecycle.is_reingest_revival`) and still reports any other `archived -> *`.
- Transitions MUST go through `service.py` or the lifecycle helpers in `_storage_lifecycle.py` — never update `status` via direct SQL.
- Use `CLAIM_STATUSES` from `models.py` as the single source of truth when writing filters or validators. Adding a new status requires updating `models.py` + `schema.sql` CHECK constraint + `schema_postgres.sql`.
- When superseding, set both `supersedes_claim_id` on the new claim AND `replaced_by_claim_id` on the old one. Steward enforces the invariant; broken pairs break the wiki.
- `ingest_claim` MCP tool does NOT currently expose `supersedes_claim_id` as a parameter — the steward closes pairs asynchronously. If you need atomic supersession, go through `auto_resolver` or `conflict_resolver` paths rather than inventing a new ingest signature.

## Authors that never auto-promote (T-1354)

`memorymaster/core/promotion_policy.py` is the single policy point: claims whose `source_agent` is in `non_auto_promotable_sources()` (default `{"llm-stop-hook", "precompact-residual"}`, override with `MEMORYMASTER_NON_AUTO_PROMOTABLE_SOURCES`, empty disables) stay `candidate`. `transition_claim` raises `PromotionPolicyBlocked` for `-> confirmed` unless `event_payload["actor"]` is `operator` or `agent`; the validator, curation drain, Jev revalidation selection and the raw-SQL `llm_steward` confirm consult the same policy. A new automatic promoter must go through `transition_claim` (or call `auto_promotion_blocked`) and must not wrap the confirm in a broad `except` that downgrades the refusal to `conflicted`.

Structure policy (T-1591), same gate: a claim with a `predicate` but no `subject` is a shifted field, not a triple, and is never auto-promoted either (`structure_blocks_auto_promotion`). `subject`+`predicate` with no `object_value` is NOT malformed (dream-worker's normal shape). The loopback/docs-URL noise is fixed at the source instead: `govern/jobs/extractor.py:infer_structure` no longer stamps `server/ip_address` for loopback addresses nor `service/url` for loopback or generic docs URLs (`GENERIC_DOCS_DOMAINS`, currently `code.claude.com` only). Existing claims' triples were not rewritten.

Customer-balance policy (T-2112), same gate: a claim that states what an identifiable customer owes, has outstanding or holds as account balance is operational billing data, not memory, and is never auto-promoted (`states_customer_balance`, shared by `claim_auto_promotion_blocked`, so `transition_claim`, the validator, `validate_now`, the curation drain's conflict winners, Jev re-confirmation and compaction summaries all hold it back, as `candidate`, except a stale claim re-confirmed by Jev, which stays `stale`; the llm steward's raw-SQL confirm and its extra-extraction inserts consult the same function). Validator and `validate_now` report it as `promotion_blocked_customer_balance` / `held: customer_balance`. The detector needs a CONJUNCTION inside one window of about 200 characters around a balance term (saldo, balance, deuda/adeuda, debt, outstanding, owe/owed, impago, unpaid, overdue, or `debe` directly followed by a currency): a currency-marked amount (`$ 12.500`, `ARS 12,500.00`, `12500 pesos`) or an explicit zero (`saldo cero`, `zero debt balance`), AND a party: a client-id marker (`cliente 9001`, `Cliente N° 9001`, `nro.`, `customer #9001`, `client <name> #9001`, `uisp 9001`, `socio 45`) anywhere in the claim (subject included), or a person-like `subject` (1-4 name tokens: Capitalised, an initial, or an ALLCAPS surname with at least two tokens; lowercase particles `de la` allowed between; no digits, no acronym such as UISP/API, no corporate suffix such as Banco/Corp/SRL) that appears in the window. Triple form: person-like subject + balance wording in the predicate + amount in `object_value` or text. A bare decimal amount without a currency (`deuda de 12500.00`) counts only right after the balance term and with a client id in the claim. A party known only by name, or a client id that is NOT next to the balance term, is excused when the window carries an anchored rule cue: a comparator or under/below/at least right before the amount (`> $0`, `under $1,000`), average/minimum/prepay/threshold right before the balance term, or a counted plural (`38 suspended clients`): that is a condition or an aggregate. A client id next to the balance term is never excused by a cue. The detector is linear: it judges the first 20,000 characters and the first 50 balance terms of a claim, with bounded regexes (a 200 KB hostile text takes milliseconds; before, 10 KB of digits took seconds). Known limits: a balance in prose with no client id and no matching subject is missed (a rule for a capitalised name found only in the prose was measured against a 25-claim sample and the live confirmed set: +1 flagged, +1 false positive, not adopted); a lowercase name is not a person; `debe 12500 pesos` (no symbol right after `debe`) and amounts without a currency marker, unless next to a client id as above, are missed; text beyond the first 20,000 characters is not judged; a non-person Capitalised subject (a project, or a company or bank without a corporate suffix) with a balance and amount in one sentence is a false positive, e.g. the user's own debt to a provider (on promotion paths it stays `candidate` and an operator/agent can still confirm it; on the atlas extractor path the row is dropped for good); the llm steward judges each candidate's own stored triple and text BEFORE selecting it (`_candidate_window`), so a held candidate neither spends the `limit` budget nor reaches the provider, and still re-checks the triple the model rewrites; MCP `ingest_claim` does not relay `held` (the returned claim shows `candidate`). Already-confirmed claims are not demoted (the holds skip `status == confirmed`), and the atlas extractor's rejected rows count in `invalid_rows`/`partial`, never `degraded`; the drop sits in `_ingest_typed_claim`, so `scripts/atlas_autoloop.py` (which calls it directly) honours it too (that would make the capture worker retry and resend the message). Not wired on purpose: `restore_declared_correction` (operator repair, the caller decides), skill approval (`knowledge/skills.py`, an explicit operator decision) and the direct `apply_status_transition` callers (`public/demo.py` fixture; `forget`/dedupe only archive). The graph-observation review skips a blocked observation instead of aborting its batch.

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
