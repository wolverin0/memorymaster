# Cross-Project Architectural Patterns

Generated from `query_meta_decisions(query="", claim_types=["decision", "architecture"], top_n=30)` on the local `memorymaster.db`.

This document is intentionally conservative. The exact query returned one qualifying concept group. The group listed 19 project scopes, but only 5 `exemplar_claim_ids`; therefore quotes are included only for those returned exemplar IDs. No claim IDs are inferred from ordinary claim lookup or invented.

## TL;DR

| Rank | Pattern | Claim count | Scope count | Returned exemplar IDs |
|---:|---|---:|---:|---|
| 1 | service | 65 | 19 | 10583, 10256, 9475, 21578, 19940 |

No other concept groups were returned in the exact top-30 query result.

## Query Contract

- Query: empty string
- Claim types: `decision`, `architecture`
- Requested top N: 30
- Qualifying filter applied in this document: `claim_count >= 3` and at least 2 `project:*` scopes
- Returned group count: 1
- Qualifying group count: 1
- Source API field for IDs: `exemplar_claim_ids`

## Pattern 1: service

The `service` pattern is a broad operational architecture cluster. It recurs across projects as decisions about runtime topology, service ownership, deployment boundaries, and production coordination.

### Shared Scopes

- `project:ColdWake`
- `project:New project`
- `project:aimigration`
- `project:app`
- `project:clawtrol`
- `project:crm-standalone`
- `project:elbraserito`
- `project:interonda`
- `project:lifeagent`
- `project:memorymaster`
- `project:mzcopilot`
- `project:omniremote`
- `project:pather`
- `project:pauol`
- `project:personaldashboard`
- `project:testproject-leads-crm`
- `project:wezbridge`
- `project:whatsapp-bot`
- `project:whatsappbot`

### Exemplar Claims Returned By The Tool

The exact query returned five exemplar IDs for the `service` group. These cover four unique scopes because `project:clawtrol` had two returned exemplars.

| Scope | Claim ID | Type | Subject | Representative quote |
|---|---:|---|---|---|
| `project:elbraserito` | 10583 | decision | service | "elbraserito final curation state (verified 2026-04-18 after coordinator takeover): single commit 0edce7f on branch `omni/fix-claude-rules-curation` containing 166 files / 25153 insertions." |
| `project:clawtrol` | 10256 | architecture | server | "ClawTrol deployment topology (as of 2026-04-17, post-audit): Rails app runs on the HOST via systemd user service `clawtrol.service` (Type=simple), reaches Postgres at 127.0.0.1:15432 via a Docker port binding added to docker-compose.yml's db service." |
| `project:wezbridge` | 9475 | decision | server | "wezbridge v2.4.0 TAGGED + stabilized 2026-04-15 (main commits 3a9c5ca, 990042e, 21e2554)." |
| `project:pather` | 21578 | decision | pather | "Pather phase-12-saas branch state 2026-04-29 22:00 ART: 7 commits ahead of main (d4e0510 schema, d3ccb7f gitignore, 16a7e9f auth provider, fc7479c scaffold, f5765ae ports + RLS tests, 50d619d lemon-webhook, f0e3019 layout polygonize fix)." |
| `project:clawtrol` | 19940 | architecture | clawtrol | "clawtrol production runtime on the home-server VM is NOT docker-compose for the Rails web tier" |

### Curated Summary

Across the returned exemplars, the recurring decision pattern is that services are documented as deployed operational systems, not just code modules. The claims emphasize branch state, runtime ownership, production verification, service process managers, database connectivity, and release gates.

The strongest repeated architectural signal is explicit runtime topology:

- `project:clawtrol` records that the Rails web tier runs under systemd user services while database connectivity is handled separately.
- `project:wezbridge` records release stabilization around an HTTP service and its security gate.
- `project:pather` records a SaaS deployment state across Vercel, Supabase Edge Functions, and webhook infrastructure.
- `project:elbraserito` records curation and repository service state as the operational truth for follow-up agents.

The pattern is useful for future project work because it treats service architecture as a living operational boundary. A cross-project memory consumer should look for this pattern before changing deploy scripts, runtime assumptions, monitoring, webhooks, or service ownership.

### Operational Themes

| Theme | Evidence from returned exemplars | Practical implication |
|---|---|---|
| Runtime topology | `project:clawtrol` distinguishes host systemd units from Docker services. | Do not assume `docker compose` is the runtime just because compose files exist. |
| Release state | `project:wezbridge` and `project:pather` include branch, commit, and tag state. | Treat claims as release gates and branch-state anchors before merging or deploying. |
| External services | `project:pather` records Vercel, Supabase, and Lemon Squeezy integration state. | Verify external platform state before changing app code that depends on it. |
| Coordination artifacts | `project:elbraserito` records `.claude`, rules, skills, and GitNexus pointers. | Agent coordination files can be part of the effective architecture. |
| Deployment mismatch | `project:clawtrol` has two exemplars warning that repository files can mislead runtime assumptions. | Prefer observed runtime claims over static repository assumptions when they conflict. |

### Scope Coverage From Exact Result

The table below distinguishes scopes that had a returned exemplar from scopes that were only present in the group's `scopes` list.

| Scope | Returned quote in this doc? | Returned exemplar ID(s) |
|---|---|---|
| `project:ColdWake` | No | None in exact `exemplar_claim_ids` |
| `project:New project` | No | None in exact `exemplar_claim_ids` |
| `project:aimigration` | No | None in exact `exemplar_claim_ids` |
| `project:app` | No | None in exact `exemplar_claim_ids` |
| `project:clawtrol` | Yes | 10256, 19940 |
| `project:crm-standalone` | No | None in exact `exemplar_claim_ids` |
| `project:elbraserito` | Yes | 10583 |
| `project:interonda` | No | None in exact `exemplar_claim_ids` |
| `project:lifeagent` | No | None in exact `exemplar_claim_ids` |
| `project:memorymaster` | No | None in exact `exemplar_claim_ids` |
| `project:mzcopilot` | No | None in exact `exemplar_claim_ids` |
| `project:omniremote` | No | None in exact `exemplar_claim_ids` |
| `project:pather` | Yes | 21578 |
| `project:pauol` | No | None in exact `exemplar_claim_ids` |
| `project:personaldashboard` | No | None in exact `exemplar_claim_ids` |
| `project:testproject-leads-crm` | No | None in exact `exemplar_claim_ids` |
| `project:wezbridge` | Yes | 9475 |
| `project:whatsapp-bot` | No | None in exact `exemplar_claim_ids` |
| `project:whatsappbot` | No | None in exact `exemplar_claim_ids` |

### Scope Notes

#### `project:ColdWake`

- Included in the `service` concept group's returned scope list.
- The exact query did not return a claim ID for this scope.
- No quote is emitted for this scope because the anti-hallucination rule requires IDs to come from `exemplar_claim_ids`.
- Future improvement: increase exemplar coverage in `query_meta_decisions` so at least one exemplar per returned scope is available.

#### `project:New project`

- Included in the `service` concept group's returned scope list.
- The exact query did not return a claim ID for this scope.
- No quote is emitted for this scope because the anti-hallucination rule requires IDs to come from `exemplar_claim_ids`.
- Future improvement: normalize placeholder-like scope names if they are not intended to be durable project scopes.

#### `project:aimigration`

- Included in the `service` concept group's returned scope list.
- The exact query did not return a claim ID for this scope.
- No quote is emitted for this scope because the anti-hallucination rule requires IDs to come from `exemplar_claim_ids`.
- Future improvement: expose per-scope exemplars for broad concept groups.

#### `project:app`

- Included in the `service` concept group's returned scope list.
- The exact query did not return a claim ID for this scope.
- No quote is emitted for this scope because the anti-hallucination rule requires IDs to come from `exemplar_claim_ids`.
- Future improvement: make generic scopes easier to disambiguate when they appear in cross-project meta summaries.

#### `project:clawtrol`

- Included in the `service` concept group's returned scope list.
- Returned exemplar IDs: 10256, 19940.
- Both exemplars describe runtime topology and deployment assumptions.
- The recurring architecture lesson is to verify the actual runtime manager before changing service operations.

#### `project:crm-standalone`

- Included in the `service` concept group's returned scope list.
- The exact query did not return a claim ID for this scope.
- No quote is emitted for this scope because the anti-hallucination rule requires IDs to come from `exemplar_claim_ids`.
- Future improvement: per-scope exemplar selection would let this doc show why the scope clustered into `service`.

#### `project:elbraserito`

- Included in the `service` concept group's returned scope list.
- Returned exemplar ID: 10583.
- The exemplar describes repository curation state, branch state, coordination files, and the PR target.
- The recurring architecture lesson is that agent coordination artifacts can become part of service reliability.

#### `project:interonda`

- Included in the `service` concept group's returned scope list.
- The exact query did not return a claim ID for this scope.
- No quote is emitted for this scope because the anti-hallucination rule requires IDs to come from `exemplar_claim_ids`.
- Future improvement: include one exemplar per scope for every returned cross-project group.

#### `project:lifeagent`

- Included in the `service` concept group's returned scope list.
- The exact query did not return a claim ID for this scope.
- No quote is emitted for this scope because the anti-hallucination rule requires IDs to come from `exemplar_claim_ids`.
- Future improvement: emit enough exemplar IDs to make each scope auditable from the summary alone.

#### `project:memorymaster`

- Included in the `service` concept group's returned scope list.
- The exact query did not return a claim ID for this scope.
- No quote is emitted for this scope because the anti-hallucination rule requires IDs to come from `exemplar_claim_ids`.
- Future improvement: make MemoryMaster's own architectural decisions visible in cross-project summaries without separate lookup.

#### `project:mzcopilot`

- Included in the `service` concept group's returned scope list.
- The exact query did not return a claim ID for this scope.
- No quote is emitted for this scope because the anti-hallucination rule requires IDs to come from `exemplar_claim_ids`.
- Future improvement: expose a representative returned claim for each scope in high-cardinality concepts.

#### `project:omniremote`

- Included in the `service` concept group's returned scope list.
- The exact query did not return a claim ID for this scope.
- No quote is emitted for this scope because the anti-hallucination rule requires IDs to come from `exemplar_claim_ids`.
- Future improvement: separate broad operational service patterns from project-specific service names.

#### `project:pather`

- Included in the `service` concept group's returned scope list.
- Returned exemplar ID: 21578.
- The exemplar describes a SaaS branch and deployment state across frontend, Supabase, Edge Functions, and webhooks.
- The recurring architecture lesson is to treat external integration state as part of the service boundary.

#### `project:pauol`

- Included in the `service` concept group's returned scope list.
- The exact query did not return a claim ID for this scope.
- No quote is emitted for this scope because the anti-hallucination rule requires IDs to come from `exemplar_claim_ids`.
- Future improvement: include enough metadata to distinguish user/workspace scopes from product scopes.

#### `project:personaldashboard`

- Included in the `service` concept group's returned scope list.
- The exact query did not return a claim ID for this scope.
- No quote is emitted for this scope because the anti-hallucination rule requires IDs to come from `exemplar_claim_ids`.
- Future improvement: make returned exemplars proportional to scope count.

#### `project:testproject-leads-crm`

- Included in the `service` concept group's returned scope list.
- The exact query did not return a claim ID for this scope.
- No quote is emitted for this scope because the anti-hallucination rule requires IDs to come from `exemplar_claim_ids`.
- Future improvement: ensure test and production project scopes can be filtered or labeled in summaries.

#### `project:wezbridge`

- Included in the `service` concept group's returned scope list.
- Returned exemplar ID: 9475.
- The exemplar describes a tagged service release and HTTP security stabilization.
- The recurring architecture lesson is that service decisions often pair release state with security gates.

#### `project:whatsapp-bot`

- Included in the `service` concept group's returned scope list.
- The exact query did not return a claim ID for this scope.
- No quote is emitted for this scope because the anti-hallucination rule requires IDs to come from `exemplar_claim_ids`.
- Future improvement: emit per-scope exemplars for chat/bot service patterns.

#### `project:whatsappbot`

- Included in the `service` concept group's returned scope list.
- The exact query did not return a claim ID for this scope.
- No quote is emitted for this scope because the anti-hallucination rule requires IDs to come from `exemplar_claim_ids`.
- Future improvement: consider scope normalization for similarly named bot projects.

## Reuse Guidance

When another project has a `service`-like architectural decision, use these checks before changing it:

1. Identify the real runtime manager.
2. Check whether deployment state is branch, tag, or external-platform dependent.
3. Verify whether repository config files match observed production state.
4. Look for service-specific security gates.
5. Treat coordination files and agent instructions as operational inputs when they affect future maintenance.

## Data Limitations

- The exact query returned only one top-level pattern, so the TL;DR table has one row.
- The `service` concept is broad and likely aggregates multiple operational subpatterns.
- The returned `exemplar_claim_ids` list is capped at five IDs.
- The returned scope list is longer than the returned exemplar list.
- This doc does not fetch substitute representative claims for missing scopes because that would violate the requested provenance rule.
- The `project:clawtrol` scope appears twice in returned exemplars, reducing unique quoted scope coverage from five to four.

## Provenance

All claim IDs referenced above came from this exact `query_meta_decisions` result:

```json
{
  "groups": [
    {
      "concept": "service",
      "claim_count": 65,
      "scopes": [
        "project:ColdWake",
        "project:New project",
        "project:aimigration",
        "project:app",
        "project:clawtrol",
        "project:crm-standalone",
        "project:elbraserito",
        "project:interonda",
        "project:lifeagent",
        "project:memorymaster",
        "project:mzcopilot",
        "project:omniremote",
        "project:pather",
        "project:pauol",
        "project:personaldashboard",
        "project:testproject-leads-crm",
        "project:wezbridge",
        "project:whatsapp-bot",
        "project:whatsappbot"
      ],
      "exemplar_claim_ids": [
        10583,
        10256,
        9475,
        21578,
        19940
      ]
    }
  ]
}
```
