<!-- doc-head: compiled user profile v1 implementation contract -->
# Compiled User Profile V1
# Covers: evidence input, weekly GLM map/reduce, deterministic projection, and injection.
# Key terms: verbatim_memories, exact supports, user.md, SessionStart, feature flag.
# Read when: changing profile extraction, lifecycle, scheduling, rendering, or rollout.
# Status: implemented on an isolated feature branch; integration and activation pending.
<!-- /doc-head -->

## Contract

MemoryMaster's SQLite database remains authoritative. The generated
`~/.memorymaster/projections/user.md` is a bounded, disposable view of stable
user facts and preferences; it is never parsed back into claims and contains
facts, not agent instructions.

- Input is incremental sanitized `verbatim_memories`: user turns are evidence;
  the preceding assistant turn is bounded context only.
- GLM map output proposes allowlisted facts with exact verbatim row IDs. GLM
  reduce output must partition every candidate into add, reinforce, replace, or
  ignore. Unknown IDs, sensitive content, malformed JSON, and instruction-shaped
  text fail closed.
- New or replacement facts require support from at least two independent
  sessions. SQLite records exact row IDs, session IDs, message hashes, and dates.
- Stable facts survive silence. Preferences expire after 90 unsupported days.
- A deterministic renderer writes at most 40 facts within an 800-token budget.
- The existing Dreaming task runs at most three map calls per invocation and
  resumes from durable watermarks. `MEMORYMASTER_COMPILED_PROFILE=1` enables it;
  the default is off.
- SessionStart injects only a bounded file carrying MemoryMaster's generated
  marker. Hand-written or oversized files are ignored.

## Operator commands

```powershell
python -m memorymaster.profile status --db .\memorymaster.db
python -m memorymaster.profile run --db .\memorymaster.db --workspace . --force
```

## Acceptance evidence

- Focused engine tests cover incremental extraction, strict support validation,
  resumability, exact support lineage, independent-session gating, preference
  expiry, stable-fact retention, and deterministic budget bounds.
- Surface tests cover feature-off scheduling, fail-closed enabled scheduling,
  generated-only SessionStart loading, and CLI status/help.
- Activation, a public package release, and historical transcript bootstrap are
  separate operator actions; this implementation does not perform them.
