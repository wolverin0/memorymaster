# Claims Lifecycle Rules

The claims DB is the source of truth. Every claim has a **status**, **tier**, **scope**, and **bitemporal fields**. Violating any of these invariants silently corrupts memory.

## Status transitions

| Status | Meaning | Set by |
|--------|---------|--------|
| `candidate` | New claim, not yet validated | `ingest_claim` (default) |
| `working` | Promoted by steward or human | `run_cycle` or manual pin |
| `active` | Stable, high-confidence truth | Steward after repeat validation |
| `superseded` | Replaced by a newer claim | `supersedes_claim_id` link |
| `archived` | Decayed or explicitly retired | Steward decay pass |

- Transitions MUST go through `service.py` — never update `status` via direct SQL.
- When superseding, set both `supersedes_claim_id` on the new claim AND `replaced_by_claim_id` on the old one. Steward enforces the invariant; broken pairs break the wiki.

## Tiers (working memory, not status)

Tiers are orthogonal to status — they control recall ordering and compaction:
- `working` — default for new candidates
- `recent` — promoted on access or ingest
- `compact` — older, summary only
- `archive` — rarely recalled

`recompute_tiers` runs on steward cycle; don't set tier manually unless you really mean it.

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
