<!-- doc-head: governed skills with catalog-first recall and optional Jev selection (decision surface skills) -->
# Governed personal skills
# Covers: personal-skill-v1 proposals, independent-session review, recall reuse, Jev skill suggestion, and staging export.
# Key terms: skill candidate, root-session lineage, approval, include_skills, SKILL.md, surface skills, MEMORYMASTER_JEV_SKILLS_ENABLED.
# Read when: reviewing workflows or integrating approved skills into agent recall.
# Safety: review is default-off, promotion is human-only, and export never activates global files.
<!-- /doc-head -->

MemoryMaster can turn a recurring, reusable workflow into a governed skill
candidate. The source of truth remains SQLite: a skill is an ordinary claim
with `claim_type=skill`, `predicate=applies_when`, and a strict
`personal-skill-v1` JSON payload.

## Lifecycle

1. Rule mining records activity in `rule_stats` and hashed root-session lineage
   in `rule_observations`.
2. A project skill becomes review-eligible after three distinct human root
   sessions. A user/global pattern also needs two projects.
3. The bounded reviewer classifies the evidence and may create a candidate.
4. The generic validator leaves every skill candidate pending.
5. An operator explicitly approves or rejects the candidate.
6. Approval confirms a new skill; update approval atomically supersedes its
   immutable parent version.
7. Confirmed skills can be recalled or rendered to MemoryMaster staging.
8. Agent surfaces may opt into a bounded per-turn `APPROVED SKILLS` section.

The reviewer is disabled unless `MEMORYMASTER_SKILL_REVIEW=1`. Its per-cycle
limit is `MEMORYMASTER_SKILL_REVIEW_LIMIT` (default 5, hard maximum 20), and
calls share the normal provider/cycle budget. `global` and legacy bare
`project` scopes are never selected automatically.

Repeated mining inside one root session increments diagnostic `event_count`
but does not increase independent support. Subagent and automation observations
cannot satisfy recurrence. Existing confirmed skills remain valid; unreviewed
legacy counters are not silently converted into independent history.

## CLI

```powershell
memorymaster --db memorymaster.db skill-inputs --scope project:memorymaster
memorymaster --db memorymaster.db skill-propose --input proposal.json `
  --scope project:memorymaster --supporting-claim-id 123
memorymaster --db memorymaster.db skill-review --claim-id 456 --action approve
memorymaster --db memorymaster.db skill-recall "release verification" `
  --scope project:memorymaster
memorymaster --db memorymaster.db skill-export --scope project:memorymaster
```

`skill-propose` accepts a JSON file or `--input -` for stdin. Approval and
rejection are idempotent and audit logged. Rejection archives the candidate;
it does not delete its payload, citations, evidence links, or history.

## MCP

The equivalent tools are `skill_inputs`, `skill_propose`, `skill_review`,
`skill_recall`, and `skill_export`. Candidate proposal and confirmed recall are
available to authenticated team transports with normal scope grants.
`skill_review` and filesystem export remain local-trusted/operator surfaces.

## Progressive recall

The public Python and MCP `recall` operations accept `include_skills=True` and
an optional `skill_limit` (default 3, maximum 10 through MCP). The result adds a
structured `skills` tuple and, for text output, an `APPROVED SKILLS` section.
Only complete confirmed skills in the requested scope are included; raw skill
JSON is removed from ordinary claim context, and the combined result shares
the caller's token budget.

Hermes enables this mode for authoritative and read-only fallback recall.
Candidate, stale, superseded, conflicted, archived, sensitive, and wrong-scope
skills remain unavailable. Ordinary public recall keeps the option off, so
existing callers and non-text output are unchanged.

## Catalog-first recall and optional System One selection

Skill recall enumerates confirmed skill IDs directly from SQLite before applying
the requested result limit. Unrelated fact/rule claims cannot displace a skill.
Every page is rehydrated through the service's tenant, scope, visibility and
sensitivity checks; malformed, replaced and temporally invalid skills are excluded.
The deterministic fallback ranks only this catalog and requires lexical overlap
for a nonempty query. This governed skill catalog remains SQLite-only.
Migration 25 adds a partial active-skill index. Reads use 256-ID pages; total
catalog enumeration and ranking scale with the number of active skills rather
than applying an arbitrary cutoff that could hide an applicable skill.

The optional TypeSafe Jev selector (decision surface `skills`, S5) uses
progressive disclosure in two logged decisions. The first asks the cookbook
gate ("does the request need a multi-step procedure?") and a Choice over the
authorized skill descriptions; a closed gate or a confident `none` means no
skill, otherwise the three most probable skills are shortlisted. The second
inspects the shortlist in detail and asks whether the chosen skill fits. It
returns an existing ID or confidently chooses none. Low confidence, a low fit
of the chosen skill, a contradictory answer, missing configuration, a timeout,
invalid output or an oversized request preserves the deterministic fallback.
Returned IDs are rehydrated and checked again after the provider call; Jev
cannot authorize, promote, edit or revive a claim.

It is **off by default**. The mode comes from the shared decisions config
(`MEMORYMASTER_JEV_SKILLS`, else `MEMORYMASTER_JEV_MODE`; see
[Jev decisions](jev-decisions.md)) and a `TYPESAFE_API_KEY` is required.
Backward compatibility: when neither `MEMORYMASTER_JEV_SKILLS` nor
`MEMORYMASTER_JEV_MODE` is set, the pre-4.9 flag
`MEMORYMASTER_JEV_SKILLS_ENABLED` (any of `1/true/yes/on`) means `live`; a
falsy value changes nothing, and `MEMORYMASTER_JEV_MODE=off` wins over it. The model is pinned to `jev-1.13.0`. No
credential is persisted by this feature. Queries the ingest scanner flags,
private catalog entries and skills whose text holds a local path (below) bypass
the external selector; every other query and skill descriptor leaves only
through the decisions egress redactor (private IPs, emails, tokens and home
paths written as paths are replaced; a surviving credential blocks the
request). Known gap: the redactor does not yet replace a home directory inside
a URL (`smb://nas/home/<user>/...`), so such a query still leaves with the
name; a skill descriptor never does, because it is withheld. Citations, scope and supporting evidence are never included.
Activation is separate from installation and from `MEMORYMASTER_SKILL_REVIEW`.

Defaults: at most 200 skills, a three-skill shortlist, gate threshold 0.50,
confidence and winner-fit thresholds of 0.70 (stored per question version in
the decisions ledger), 4,000 query characters (each field is cut to 1,200
characters by the redactor) and a 64 KiB catalog payload. Larger or incomplete
descriptors fall back without truncating safety conditions, and so does a skill
whose text holds a local path (drive, UNC or system/home directory, also inside
a URL such as `smb://nas/home/<user>` or `https://host/users/<name>`; only the
`s:/` of `https://` is not a drive): one such skill keeps the whole catalog
local until it is fixed, and the logged row names it (`withheld_ref`). Both rounds use the
in-process decisions transport: no child process (so a module planted in the
working directory never sees the key), no proxy variables, no redirects, a
128 KiB response cap and the 900 ms hook deadline per round with no retries;
the legacy selection is returned at once when a deadline passes and a late
answer never changes it. Every decision, including fallbacks and requests
withheld before sending (scanner-flagged query, private catalog, size limits,
invalid or path-bearing descriptors), is written to the decisions ledger
(`decisions.db`); a withheld row carries only the rule's label, never the query
or catalog text.

This selects MemoryMaster's governed skills; it does not rewrite or activate the
agent's installed filesystem skill catalog. Local tests use disposable SQLite and
fake HTTP responses. They establish routing/fallback and governance, not JEV's
semantic quality or latency. A held-out task benchmark is still required before
claiming a usefulness improvement or changing default activation.

Official interface references: [typed questions](https://docs.typesafe.ai/primitives),
[skill suggestion](https://docs.typesafe.ai/cookbooks/skill_suggestion), and
[versioned models](https://docs.typesafe.ai/models).

## Staging boundary

`skill-export` defaults to `~/.memorymaster/staging/skills`. Every generated
`SKILL.md` header records the claim ID, exact scope, content SHA-256, skill
version, and citations. MemoryMaster does not copy these files into
`~/.claude`, `~/.codex`, `$HERMES_HOME`, or any other active instruction tree.
Activation is a separate previewed operator action.
