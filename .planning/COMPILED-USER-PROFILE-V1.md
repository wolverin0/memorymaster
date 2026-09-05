<!-- doc-head: compiled Gemini profile contract and scheduled-launcher activation discrepancy -->
# Compiled User Profile V1
# Covers: evidence input, bounded Gemini map/reduce, deterministic projection, and injection.
# Key terms: verbatim_memories, exact supports, user.md, SessionStart, feature flag.
# Read when: changing profile extraction, lifecycle, scheduling, rendering, or rollout.
# Status: shipped in 4.7; scheduled launcher enables generation; latest logged result is not_due.
<!-- /doc-head -->

## Contract

MemoryMaster's SQLite database remains authoritative. The generated
`~/.memorymaster/projections/user.md` is a bounded, disposable view of stable
user facts and preferences; it is never parsed back into claims and contains
facts, not agent instructions.

- Input is incremental sanitized `verbatim_memories`: user turns are evidence;
  the preceding assistant turn is bounded context only.
- Configured map output proposes allowlisted facts with exact verbatim row IDs. Configured
  reduce output must partition every candidate into add, reinforce, replace, or
  ignore. Unknown IDs, sensitive content, malformed JSON, and instruction-shaped
  text fail closed.
- New or replacement facts require support from at least two independent
  sessions. SQLite records exact row IDs, session IDs, message hashes, and dates.
- Stable facts survive silence. Preferences expire after 90 unsupported days.
- The profile engine renders at most 60 facts within a 1,400-token budget
  by default; explicit environment overrides still win. The original 800/40
  limits are historical. Scheduled and direct construction share defaults.
- The existing Dreaming task runs at most three map calls per invocation and
  resumes from durable watermarks. `MEMORYMASTER_COMPILED_PROFILE=1` enables it;
  the default is off.
- SessionStart injects only a bounded file carrying MemoryMaster's generated
  marker. Hand-written or oversized files are ignored.

## Operator commands

```powershell
python -m memorymaster.profile status --db .\memorymaster.db
python -m memorymaster.profile run --db .\disposable-profile.db --workspace .
```

## Acceptance evidence

- Focused engine tests cover incremental extraction, strict support validation,
  resumability, exact support lineage, independent-session gating, preference
  expiry, stable-fact retention, and deterministic budget bounds.
- Surface tests cover feature-off scheduling, fail-closed enabled scheduling,
  generated-only SessionStart loading, and CLI status/help.
- Shipped source and runtime activation remain distinct. The September 5
  artifact records its own process flag off, 52 active facts and 565 supports with zero
  mismatches. The scheduled launcher overrides generation to on and logs not_due;
  the artifact alone cannot establish worker activation. No profile rebuild is requested.
- September 5 regression: scheduled configuration silently used 800/40 while
  direct configuration used 1400/60. The local fix reads the shared defaults;
  tests compare every field and preserve explicit overrides.
- Current provider defaults come from the configured Antigravity client;
  GLM names in August receipts are historical. See ROADMAP.md for status.
