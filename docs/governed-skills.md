# Governed personal skills
# Covers: personal-skill-v1 proposals, review, progressive recall reuse, and staging export.
# Key terms: skill candidate, approval, supersession, include_skills, APPROVED SKILLS, SKILL.md.
# Read when: reviewing workflows or integrating approved skills into an agent recall surface.
# Authority: skills remain ordinary governed claims; this guide does not bypass lifecycle policy.
# Safety: review is default-off, promotion is human-only, and export never activates global files.
# Updated: 2026-08-08 after progressive confirmed-skill recall and Hermes integration.

MemoryMaster can turn a recurring, reusable workflow into a governed skill
candidate. The source of truth remains SQLite: a skill is an ordinary claim
with `claim_type=skill`, `predicate=applies_when`, and a strict
`personal-skill-v1` JSON payload.

## Lifecycle

1. Rule mining records corrections and their `correction_count`.
2. A skill becomes review-eligible after at least two observations.
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

## Staging boundary

`skill-export` defaults to `~/.memorymaster/staging/skills`. Every generated
`SKILL.md` header records the claim ID, exact scope, content SHA-256, skill
version, and citations. MemoryMaster does not copy these files into
`~/.claude`, `~/.codex`, `$HERMES_HOME`, or any other active instruction tree.
Activation is a separate previewed operator action.
