<!-- doc-head: compiled Gemini profile contract and scheduled-launcher activation discrepancy -->
# Compiled User Profile V1
# Covers: evidence input (verbatim, then governed claims in 4.9.0), bounded map/reduce, projection, injection.
# Key terms: verbatim_memories, claims source, tenant filter, claim egress, export_refused (_DREAM_EXTRA_PATTERNS), claim:unlinked, memory_claim, retraction, freshness WARN.
# Read when: changing profile extraction, lifecycle, scheduling, rendering, or rollout.
# Status: shipped in 4.7; 4.9.0 (F-03) adds the claims source, hardened per the 2026-09-23 operator decisions.
<!-- /doc-head -->

## Contract

MemoryMaster's SQLite database remains authoritative. The generated
`~/.memorymaster/projections/user.md` is a bounded, disposable view of stable
user facts and preferences; it is never parsed back into claims and contains
facts, not agent instructions.

- Input is incremental sanitized `verbatim_memories`: user turns are evidence;
  the preceding assistant turn is bounded context only. When no new verbatim
  input exists, governed claims feed the profile (see "Claims evidence").
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

## Claims evidence (4.9.0, review F-03)

Verbatim capture is opt-in, so `verbatim_memories` stopped growing on
2026-08-24 and the profile froze. It kept being injected as current. A run
now uses verbatim input when new rows exist, else governed claims.

- Eligible: `confirmed` claims, and `candidate` claims from `dream-worker`.
  They must be public, not replaced, temporally current, and pass
  `scan_persisted_value` with no `[REDACTED` marker and no
  `[ERASED_CLAIM_TEXT]` payload (`redact_claim_payload` keeps the status).
- Tenant: selection and support resolution use the service tenant with the
  store's `tenant_id IS ?` semantics; legacy NULL-tenant rows only when the
  tenant is unset. Claims of another tenant never become supports.
- Egress (`profile/egress.py`): the text sent to the map provider is redacted
  (`redact_text` credentials, private IPv4 including bare IPs in prose,
  Windows/UNC paths, POSIX, Git Bash, WSL `/mnt/c/Users` and `~user/` home
  paths, email). The claim is then blocked, not sent and not left pending,
  when a credential-grade finding survives: a secret pattern in any encoded
  variant, or a match of dream_bridge's `_DREAM_EXTRA_PATTERNS` (the same
  compiled pattern: `DB_PASS=`-style assignments, Supabase/SendGrid/Twilio
  keys, webhook tokens, SSH command shapes, public `IP:port`, an existing
  `[REDACTED` marker).
- Prompt: claim items carry their text as `memory_claim`, labelled a
  third-party memory assertion, not the operator's own words.
- Selection: eligible claims that no run has mapped, recorded in
  `compiled_profile_claim_seen` (migration 0027) in the same transaction as
  their candidates. This is not a MAX(id) watermark, so claims confirmed later
  (steward, S1 re-confirmation, conflict resolution) are still read.
- Supports store the claim id negated in `verbatim_id`, so exact manifests and
  the mismatch check stay shared. A claim that loses eligibility before reduce
  is rejected as a support.
- Retraction: every engine run, including a `not_due` one, re-checks the
  claim supports of active facts before the cadence gate. Supports whose claim is no longer eligible are removed; a
  fact left below the session gate is retired as `expired` and the projection
  is rewritten. A removed claim leaves the seen set, so it is mapped again if
  it becomes eligible again.
- Session unit for the two-session gate: the Dreaming session hash, else one
  normalized `session` citation locator. Claims with no such lineage share
  one bucket, `claim:unlinked`, so they cannot pass the gate by themselves; a
  claim citing several sessions is its own unit.
- Operational review: `compiled_profile` WARNs (exit 3) when the newest
  active-fact support is older than 7 days, compared as a real (float) age.
  Clock skew under a day counts as age 0.
- Steward actor (F-21): lifecycle events caused by a proposal resolution carry
  its `actor` in the payload; those the steward cycle applies itself carry
  `actor: automation`.

Still open (not covered by the 2026-09-23 decisions): no scope or
`claim_type` filter (skills and rules are included), and the first claims run
covers the whole eligible backlog.

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
