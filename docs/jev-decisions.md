<!-- doc-head: user guide for TypeSafe Jev decisions and the decision ledger (memorymaster.decisions, 4.9.0) -->
# Jev decisions
# Covers: what Jev decides (surfaces S1-S8, S3 R8 question versions), modes, env flags, fallbacks, breaker probes, hook bounds, never-act-unlogged, skip rows, ledger, retention, stdlib transport, egress coverage.
# Key terms: surface, mode, legacy action, fallback_reason, ledger_unavailable, breaker_open probe, skip:<reason>, record_skip, fallback_record, exploration, propensity, decisions.db, egress_blocked, home_path, phone, withheld, label_source, apply_failed, MEMORYMASTER_JEV_SKILLS_ENABLED (pre-4.9 flag).
# Read when: enabling or disabling Jev, auditing what left the machine, reading decisions.db, logging a surface skip, or exporting decisions for calibration/OPE.
# Safety: default off; SQLite stays authoritative; live actions are reversible or proposals; unredactable text is never sent.
<!-- /doc-head -->

MemoryMaster can ask TypeSafe **Jev** (System One model `jev-1.13.0`) short,
literal questions at a few decision points: *is this memory relevant to this
request?*, *is this stale claim still correct?*, *does this candidate have
evidence?* Jev answers with probabilities; ordinary code turns them into an
action. Every decision, including every fallback, is written to a local
append-only ledger so it can be measured, calibrated and audited later.

Nothing happens until you turn it on. With no configuration the mode is `off`:
no request is sent, no HTTP client is loaded and no ledger row is written.

## Guarantees

1. **SQLite stays authoritative.** Jev only orders or labels items that code has
   already authorized (scope, tenant, visibility, sensitivity, status). Any item
   Jev's policy names that code did not authorize is rejected (`choose_invalid`),
   and so is a ranking whose scores are missing or not finite numbers.
2. **Live actions are reversible or proposals.** Jev never deletes, archives,
   degrades, merges or supersedes anything by itself.
3. **Deterministic fallback, always.** Timeouts, HTTP errors, malformed answers,
   blocked egress, an open breaker, an exhausted budget or a missing key all
   return the existing (legacy) behaviour, and the reason is logged.
   A late answer never changes an action.
4. **No private data leaves unredacted.** Everything sent goes through one
   redactor; if a credential survives redaction the request is not sent.
5. **Everything is logged, append-only**, including fallbacks, blocked egress and
   shadow decisions. An unexpected internal error (`engine_error`) is logged too,
   with the request's transport outcome, tokens and cost, so the budget and the
   breaker still count it.

## What is decided

A surface only makes decisions once its integration is installed; until then the
engine is idle for that surface.

| Surface (env id) | Question Jev answers | Live action | Never does | Exploration |
|---|---|---|---|---|
| S1 `REVALIDATE` | Is this stale claim still correct, lasting and useful? | re-confirm stale -> confirmed (a normal lifecycle event); never while promotions are frozen by a failed integrity check, for a claim with an unresolved supersession proposal, for an observation, or when the validator score would fall below the decay stale threshold; a private claim or one the sensitivity scan flags is never asked (it stays stale) | archive or downgrade | none |
| S2 `RECALL` | Per candidate memory: relevance (4 levels), usable evidence, conflicts with the request, reads like an instruction | order the authorized public memories and pick 2-5 to inject; conflicting/instruction-like items are labelled, not hidden; private or sensitive memories the caller may read are never sent and keep their legacy place (`passthrough`) | inject an unauthorized memory, drop an authorized one | 10 %: top-5 order sampled (Plackett-Luce) |
| S3 `INGEST` | Eight checks per Dreaming candidate (evidence, chronology, modality, scope, specificity, privacy, usefulness, novelty) | admit, or mark `held` (kept, releasable; its capture is exempt from Dreaming retention for `MEMORYMASTER_DREAM_HELD_RETAIN_DAYS` after the hold, never while quarantined, then pruned with a `held_expired` outcome) | discard | 5 % of held candidates admitted anyway |
| S4 `DEDUP` | Same fact (3 levels), contradiction, supersession (asked two ways), same scope | write a steward proposal tagged `source: jev` (`steward_proposal:jev_*`, so ranking ignores it until an operator approves); a pair with a private or sensitive claim is never asked | change status; proposals are never auto-approved, and the MCP tool cannot resolve them: only the operator does (dashboard, or `memorymaster resolve-proposal --actor operator`) | none |
| S5 `SKILLS` | Does the request need a procedure; which skill, if any; does it fit | suggest 0-1 skill | load a skill that is not authorized | none |
| S6 `HINTS` | Seven prompt signals (decision, constraint, bug root cause, environment, reference, architecture, preference) | which ingest hints to show | ingest anything | none |
| S7 `ROUTE` | Query type (7 types + `unknown`) | retrieval route; `unknown` keeps the keyword rules | skip authorization | none |
| S8 `SESSION` | Relevance of each recent confirmed claim to the project | which public claims fill the 5 SessionStart slots; a private or sensitive claim keeps its legacy slot and is never sent | inject stale/candidate claims | none |

**S5 skills** asks in two logged decisions (`baseline_features.round` is
`wide` or `detailed`; the second names the first as `wide_decision_id`): the
procedure gate plus a Choice over every authorized skill's short description,
then, for the three most probable skills, a detailed Choice plus a fit question
per skill. A closed gate or a confident `none` suggests nothing; low confidence
or a low fit of the chosen skill keeps the lexical selection. Items are
`claim:<id>`. A request withheld before the engine is never sent but is still
logged as one fallback row (`not_sent`, no request or catalog text) whose
`baseline_features.withheld` names the rule, plus `withheld_ref` when one skill
caused it: `query_sensitive` (the ingest scanner flags the query) and
`catalog_private` (a non-public skill) and `descriptor_path` (a skill text holds
a local path, also a home or system directory inside a URL) log `egress_blocked`; `query_too_long`,
`catalog_too_large` (over 200 skills) and `payload_too_large` (over 64 KiB) log
`request_too_large`; `query_empty`, `catalog_empty` and `descriptor_invalid`
(an incomplete or oversized descriptor) log `invalid_request`. One such skill
withholds the whole catalog until it is fixed, which `withheld_ref` makes
visible. `delivered` is what `recall_skills` re-authorized against SQLite, so a
pick retired mid-call is exposed but not delivered. Details:
[governed skills](governed-skills.md).

**S6 hints** runs in the `UserPromptSubmit` classify hook: one request of seven
nouls over the redacted prompt (meta wrappers stripped). Hints are the labels at
or above their `show` threshold (0.7); the regex classifier is the legacy action
and the fallback. Items are `hint:<name>`; exposure is what was shown. Like every surface (S5 included), it logs `session_key` as the normalized, tenant-bound session hash the recall hooks and the Stop-hook joiner use, never the raw session id. `GOTCHA`
has no Jev question, so only the regex fallback can show it, and `PREFERENCE`
only Jev can show. With no Jev variable set the hook does not load the engine
and its output is byte-identical to the regex-only hook.

**S3 ingest questions (ruling R8).** Operator preferences used to be held
because two checks assumed a project and treated the operator as a third party.
Current wordings: `ingest.privacy` v2 *Is `candidate` free of secrets or
credentials and of personal data about people other than the operator?* and
`ingest.usefulness` v2 *Would `candidate` help a coding agent in a future
session with this operator or on this project?* (v1 stay registered for
history). A `personal` candidate is asked the personal variant of `ingest.scope`
(v2) *Is `candidate` about the operator's own stable preferences, working style,
tools, environment or constraints?*; project candidates keep `ingest.scope` v1.
The variant owns `ingest.scope` v2 (answers are logged under the id `ingest.scope`
with version 2), so the next default wording of `ingest.scope` must be v3; the
registry refuses a second v2 at import.

Questions are versioned; changing a question's wording creates a new version
with its own thresholds. Ages and counts are computed in code and passed as
named buckets, never as dates for Jev to reason about.

## Modes and how to turn it off

| Mode | Sends requests | Action taken | Logged |
|---|---|---|---|
| `off` (default) | no | legacy | nothing (unless `MEMORYMASTER_DECISIONS_LOG_OFF=1`) |
| `shadow` | yes | legacy | Jev's answers and the action Jev would have taken |
| `live` | yes | Jev's policy (with logged exploration) | everything |

```bash
# Everything off (the default): unset the variable or
export MEMORYMASTER_JEV_MODE=off

# Everything live, but keep recall in shadow and switch ingest off
export MEMORYMASTER_JEV_MODE=live
export MEMORYMASTER_JEV_RECALL=shadow
export MEMORYMASTER_JEV_INGEST=off
```

Per-surface variables are `MEMORYMASTER_JEV_<SURFACE>` with the ids above.
An unknown value is treated as `off`. Flags accept `1/true/yes/on` (any case) as
true; anything else is false.

**Pre-4.9 flag.** `MEMORYMASTER_JEV_SKILLS_ENABLED` still works: when neither
`MEMORYMASTER_JEV_SKILLS` nor `MEMORYMASTER_JEV_MODE` is set, a true value means
`skills=live` (what the flag did before); a false value changes nothing.
Precedence for `skills` is `MEMORYMASTER_JEV_SKILLS` > `MEMORYMASTER_JEV_MODE` >
`MEMORYMASTER_JEV_SKILLS_ENABLED` > `off`, so `MEMORYMASTER_JEV_MODE=off` turns
skills off even while the old flag is set. Prefer the new variables.

| Variable | Default | Meaning |
|---|---|---|
| `MEMORYMASTER_JEV_MODE` | `off` | global mode |
| `MEMORYMASTER_JEV_<SURFACE>` | global mode | per-surface mode |
| `MEMORYMASTER_JEV_DAILY_USD_CAP` | `2.0` | spend cap per UTC day, summed from the ledger |
| `MEMORYMASTER_JEV_RPM_CAP` | `600` | requests per minute (ledger-wide and per process) |
| `MEMORYMASTER_JEV_HOOK_DEADLINE_MS` | `900` | hard deadline for hooks (no retries) |
| `MEMORYMASTER_JEV_BATCH_DEADLINE_MS` | `8000` | deadline for batch jobs (backoff on 429/529, max 3 retries) |
| `MEMORYMASTER_JEV_EXPLORE_<SURFACE>` | recall `0.10`, ingest `0.05`, others `0` | exploration rate |
| `MEMORYMASTER_DECISIONS_DB` | `~/.memorymaster/decisions.db` | ledger location |
| `MEMORYMASTER_DECISIONS_STATE_RETENTION_DAYS` | `180` | how long redacted request text is kept |
| `MEMORYMASTER_DECISIONS_LOG_OFF` | unset | also log a minimal row while `off` |
| `MEMORYMASTER_JEV_REVALIDATE_PER_CYCLE` | `500` | stale claims S1 asks about per run of the scheduled steward-cycle hook (`0` disables) |
| `MEMORYMASTER_JEV_DEDUP_PER_CYCLE` | `200` | candidate pairs S4 asks about per run of the scheduled steward-cycle hook (`0` disables); `run_cycle` itself (MCP, CLI, per-turn operator cycle, scheduler) never asks Jev |
| `MEMORYMASTER_DREAM_HELD_RETAIN_DAYS` | `30` | days a Dreaming capture holding S3-held candidates is kept past its normal retention (ruling R7; quarantined captures are never exempt) |
| `MEMORYMASTER_JEV_BREAKER_PROBE_S` | `60` | while a breaker is open, seconds between shadow probes on that surface |
| `MEMORYMASTER_DECISIONS_HOOK_BUSY_MS` | `250` | longest a hook decision waits for another writer of `decisions.db`, per write |
| `TYPESAFE_API_KEY` | unset | API key; on Windows also read from the user environment (`HKCU\Environment`) |

The API key is never logged, never passed on a command line and never included
in an error message. Without a key every decision falls back (`missing_key`).

## Fallbacks, budget and breaker

Each fallback returns the legacy action and records `fallback_reason`:
`missing_key`, `breaker_open`, `egress_blocked`, `budget_exhausted`,
`ledger_unavailable`, `timeout`, `late`, `http_401`, `http_403`, `http_422`,
`http_429`, `http_529`, `http_5xx`, `http_4xx`, `redirect_refused`, `too_large`,
`network_error`, `malformed`, `transport_unavailable`, `request_too_large`,
`choose_error`, `choose_invalid`, `invalid_request`, `engine_error`.

- **Budget.** Before sending, the day's spend in the ledger plus the estimated
  cost of the request must stay under the daily cap, and the request rate under
  the RPM cap; otherwise `budget_exhausted`. If the ledger cannot be read the
  engine does not spend (`ledger_unavailable`); the same holds when the stored
  question thresholds cannot be read (they are never silently replaced by the
  code defaults).
- **Breaker.** Per surface, over the last 10 minutes only: 5 consecutive
  request failures, or more than 30 % failures among at least 10 requests,
  open the breaker for 15 minutes. While open the surface takes the legacy
  action (`breaker_open`) and sends at most one shadow **probe** per
  `MEMORYMASTER_JEV_BREAKER_PROBE_S` (60 s): only when no request was sent on that
  surface for that long, claimed in the ledger so every hook process agrees.
  Every other call is logged with `attempt_count` 0, no request and exploration
  arm `fallback` (a probe that was sent is arm `shadow`), so a provider outage
  costs a hook or a Dreaming candidate no deadline wait. A breaker the engine
  cannot read (another process held the ledger past the hook bound) counts as
  open: nothing is sent and the row says `ledger_unavailable`.
- **Never act unlogged.** If the decision row cannot be written, the decision
  returns the legacy action with `ledger_unavailable` (no Jev action, no
  answers), even when Jev had answered: nothing is ever done that the ledger does
  not show. If the ledger refuses the one-time question registration, no request
  is sent at all. Registration reads first, so a process whose questions are
  already stored opens no write transaction for them. Text that is not valid
  UTF-8 (a lone surrogate from a `\ud800` JSON escape) is stored escaped, so it
  never costs a decision its row.
- **Hooks are bounded.** A hook decision (every kind except `batch`) returns
  within its deadline + 100 ms, even while another process holds the ledger's
  write lock: each of its ledger opens, reads and writes that meets another
  connection's lock is retried for at most `MEMORYMASTER_DECISIONS_HOOK_BUSY_MS`
  (and never past the deadline + 100 ms). Reads do not wait for a writer's
  transaction, but can still meet a lock while other hook processes open or
  close the ledger, which is why they are retried too. Egress redaction is CPU
  work before the send: each distinct subject is redacted once (a recall
  candidate is the subject of four questions), and once the deadline has passed
  redaction stops between subjects, nothing is sent and the row says `timeout`
  (the overshoot is at most the one value being redacted). Its request only gets the
  time left before the deadline, counted from the start of the decision. Failed
  writes, and failed breaker, budget and threshold reads, are counted in
  `ledger_write_failures()`.
- **Late answers.** When the deadline passes the legacy action is returned at
  once. Every socket operation's timeout is the time left before the same
  deadline, so most slow requests end as a timeout and only a `late_answer`
  outcome is recorded. An answer that
  still arrives (for example a response already in flight) is stored for
  measurement only. A late answer never changes an action.

## Logging a skip (for surface code)

A surface that decides not to ask Jev at all (a prompt too short, a request it
never builds) still leaves a row, through public APIs only:

```python
from memorymaster.decisions.engine import default_engine

default_engine().record_skip("recall", reason="short_prompt", legacy_action=["claim:12"],
                             session_key=session_key, state={"request": prompt}, items=["claim:12"])
```

- `DecisionEngine.record_skip(surface, *, reason, legacy_action, session_key=None,
  state=None, items=())` writes one decision row with `fallback_reason`
  `skip:<reason>`, `attempt_count` 0, `transport_outcome` `not_sent`, the
  surface's current mode and the legacy action taken (propensity 1, arm
  `fallback`). `items` become item rows, exposed when the legacy action names
  them. `state` is stored only after the egress redactor (and not at all when it
  cannot be redacted). It never sends, never raises (returns whether the row was
  written), waits at most `MEMORYMASTER_DECISIONS_HOOK_BUSY_MS` for another writer,
  and in `off` writes nothing unless `MEMORYMASTER_DECISIONS_LOG_OFF` is set.
- `fallback_record(decision_id, surface, *, mode, reason, legacy_action, ...)`
  and `fallback_item_rows(decision_id, refs, *, kind, delivered)` build the rows
  of a fallback that sent nothing, for code that writes its own (the skills
  surface logs its withheld requests this way).

## What is logged

The ledger is a separate SQLite file (WAL, safe for several processes), not the
memory database. It is telemetry: deleting it (with no MemoryMaster process
running) loses history but no memory.

| Table | One row per | Contains |
|---|---|---|
| `decisions` | decision | surface, mode, question set and hash, model requested and **served**, transport outcome, fallback reason, transport latency (`latency_ms`) and whole-engine time (`engine_ms`), tokens, cost, legacy action, Jev's action, action taken, exploration arm, the full action distribution and chosen propensity, thresholds, baseline features, hashed state, redacted state, redaction counts |
| `decision_items` | item x question | answer, full probability distribution, confidence, legacy and final rank, exposed, delivered |
| `outcomes` | observed result | e.g. `used_in_turn`, `steward_confirmed`, `revalidated`, `stale`, `superseded`, `archived`, `held_later_confirmed`, `held_expired`, `consolidation_applied` / `consolidation_rejected` (the Dreaming consolidator's verdict on an S3-triaged candidate), `dream_claim_created` (links a claim a Dreaming application created, item `claim:<id>`, to its S3 decision so the claim's lifecycle outcomes join that decision), `skill_invoked`, `late_answer`, `apply_failed` (the live action in `action_taken` was not applied by the store, with the reason); with `label_source` and lag |
| `question_versions` | question version | exact wording hash and current thresholds |
| `watermarks` | joiner / breaker | progress markers |

Decision, item and outcome rows are never updated or deleted; the database
enforces this. The only exception is retention: after `MEMORYMASTER_DECISIONS_STATE_RETENTION_DAYS` the
redacted state text is set to NULL (hashes, answers and outcomes stay). Run it
with `memorymaster.decisions.ledger.prune_job()`.

**Label sources.** `detector` (a memory's id or a distinctive 8-word phrase from
it appeared in the assistant reply or tool inputs), `steward`, `automation`
(an `automation` actor or a `steward_automation:` status change), `operator`
(an explicit `operator` actor), `unknown_actor` (a `steward_human_override:`
status change with no actor: written before actors were recorded, when automatic
approvals used that prefix too), `unattributed_override` (older approvals
recorded as human overrides without an actor); neither of the last two is usable
as human ground truth; `jev` (Jev's own actions, including `jev_revalidation:`;
never used to grade Jev); `surface` (`apply_failed`, written by the surface
that could not apply its own decision). Only real status changes become lifecycle outcomes: a
proposal or a same-status confidence write does not.

## Privacy: what leaves the machine

Only the question texts, the redacted request/state and the redacted candidate
texts are sent, to the pinned endpoint `https://api.typesafe.ai/v1/systemone`.

**Transport.** The request goes over the standard library (`http.client` with
`ssl.create_default_context()`: the operating system's trust store, certificate
and hostname verification on); no third-party HTTP client is imported, which
keeps a cold hook well inside its deadline. If the TLS handshake fails
certificate verification and `certifi` is installed, that connection is retried
once with certifi's bundle; whichever context completed a handshake is reused for
the rest of the process. Proxy variables and `.netrc` are never read, redirects
are never followed (a 3xx is `redirect_refused`), and the API key is sent only in
the `Authorization` header. Responses are capped at 128 KiB, read in bounded
pieces with the time left before the deadline as each read's timeout. Idle
keep-alive connections are reused for up to 15 s, one request per connection at
a time; a connection that failed, that the server closed or that sat idle longer
(a NAT or firewall may have dropped it silently) is replaced. The ledger records
`transport_version` `stdlib-inproc/2` and `sdk_version`
`stdlib-http.client/py<major>.<minor>`. `error_class` holds only an exception's
type name, never its text.

Before sending, the redactor (`memorymaster/decisions/egress.py`) replaces
exactly these, each with a `[REDACTED:<kind>]` marker counted in
`redaction_counts_json`:

- **Secrets and credentials** (`core.security` patterns): API keys, tokens,
  passwords, PINs, connection strings, card numbers; short numeric credentials
  after a keyword (`pin 4417-2291`); credentials in URLs (userinfo and
  `?key=`/`?token=`-style query values); opaque tokens of 20+ characters near a
  secret keyword or with high entropy.
- **Private addresses**: IPv4 RFC 1918, CGNAT and link-local; IPv6 ULA and
  link-local; internal host names (`.local`, `.lan`, `.internal`, `.corp`,
  `.home`, `.home.arpa`, `.intranet`, `.localdomain`).
- **Email addresses**, also percent-encoded (`alice%40example.com`, a token is
  decoded and replaced whole).
- **Phone numbers** (`phone`), also right after a label or inside a link
  (`tel:+54...`, `WhatsApp:+54...`, `Cel:11-...`, `tel.011 ...`,
  `[llamar](tel:+54...)`), but never glued to a digit's `.`, `:` or `/`:
  - E.164: `+`, a country code that does not start with 0, then 8 to 15 digits
    in all with spaces, dots, hyphens or parentheses between groups
    (`+54 9 11 1234-5678`, `+5491112345678`, `tel:+54-9-11-1234-5678`,
    `+1 415 555 2671`);
  - Argentine national formats with an area code or mobile prefix, separated
    by hyphens or spaces, that make exactly 10 national digits once the trunk
    `0` and the mobile `15` are dropped, where the only two-digit area code is
    `11` (`011 4567-8901`, `011 4567 8901`, `(011) 4567 8901`,
    `(0351) 456-7890`, `0351-456-7890`, `0351 4567890`, `351 456 7890`,
    `11-1234-5678`, `11 1234 5678`, `11-15-1234-5678`); a parenthesized area
    code also covers `(415) 555-2671`; five-digit area codes with a six-digit
    local number (`02944 45-6789`, `(02944) 456789`); a local mobile with the
    `15` prefix and no area code, written with hyphens (`15-1234-5678`,
    `15-456-7890`);
  - WhatsApp links, whose digits carry no `+`: `wa.me/<n>` and `?phone=<n>`;
  - percent-encoded forms of any of the above (`tel:%2B5491112345678`), decoded
    like home paths and replaced whole.
  Not covered: a bare run of digits with no `+`, label or separators
  (`1145678901`, `1512345678`); a local number with no area code or `15`
  prefix (`4567-8901`, which cannot be told apart from a range such as
  `1200-1500`); a `15` mobile separated only by spaces (`15 1234 5678`); dots
  as separators in national numbers (`11.1234.5678`). A few number runs that
  are not phones are redacted as well (`sizes 128 256 1024`): over-redaction,
  never a leak.
- **Home directories** (`home_path`). In every spelling below the whole user
  name is hidden, including apostrophes between letters, dots and hyphens
  (`dan_o'neil`, `o'brien-smith.jr`, `j.doe`, `mary-jane`; each spelling is
  tested with each of these). A Windows or path user name of two or three
  space-separated words (`John Smith`) is taken whole only when a path
  separator follows it (`C:\Users\John Smith\repo`); at the very end of the
  text `C:\Users\John Smith` hides `John` and keeps ` Smith`, because the rest
  of a sentence cannot be told apart from a spaced name. A closing `)`, `]`,
  `}` or backtick after the name is kept. The spellings (shown as they appear
  in the text; "JSON-escaped" means every backslash doubled):
  - Windows `C:\Users\<u>`, `C:/Users/<u>`, JSON-escaped `C:\\Users\\<u>`,
    `C:\Documents and Settings\<u>`; rooted without a drive `\Users\<u>` (also
    JSON-escaped); relative `..\..\Users\<u>`, `.\Users\<u>` (any depth);
    MSYS2 and Cygwin homes `C:\msys64\home\<u>`, `C:/cygwin64/home/<u>` (also
    `msys`, `msys32`, `cygwin`); Git Bash `/c/Users/<u>`; WSL
    `/mnt/<drive>/Users/<u>`;
  - WSL seen from Windows, backslash, JSON-escaped or slash spelling, with
    `wsl$` or `wsl.localhost`: `\\wsl$\<distro>\home\<u>`,
    `\\wsl.localhost\<distro>\home\<u>`,
    `\\wsl$\<distro>\mnt\<drive>\Users\<u>`, `//wsl$/<distro>/home/<u>`;
  - POSIX `/home/<u>`, `/var/home/<u>`, `/export/home/<u>`, macOS
    `/Users/<u>` and `/Volumes/<disk>/Users/<u>` (a disk name of up to four
    words: `/Volumes/Macintosh HD/Users/<u>`), any-case `/users/<u>` (not REST
    placeholders like `/users/:id` or `/users/{id}`), `~<u>`;
  - NAS layouts `/volume<N>/homes/<u>`, `/share/homes/<u>`, `/homes/<u>`;
  - relative, at any depth: `.` or `..` segments before the POSIX, Git Bash,
    WSL-mount and NAS spellings (`./home/<u>`, `../../home/<u>`,
    `./../home/<u>`, `x/../home/<u>`, `../../mnt/c/Users/<u>`,
    `../../volume1/homes/<u>`);
  - UNC, host included, plain or JSON-escaped: `\\<host>\home\<u>`,
    `\\<host>\homes\<u>`, `\\<host>\Users\<u>`, the hidden shares
    `\\<host>\home$\<u>` and `\\<host>\users$\<u>`, and the same under one
    share or an admin share (`\\<host>\c$\Users\<u>`,
    `\\<host>\<share>\home\<u>`); slash-UNC `//<host>/home/<u>`,
    `//<host>/Users/<u>`, `//<host>/home$/<u>`, `//<host>/c$/Users/<u>`,
    `//<host>/<share>/home/<u>`;
  - inside URLs, scheme and host included: `smb://<host>/home/<u>`,
    `file://<host>/homes/<u>`, UNC file URLs `file:////<host>/home/<u>` and
    `file://///<host>/home/<u>`, `http(s)://<host>[:<port>]/home/<u>`,
    `.../Users/<u>`, the hidden shares `.../home$/<u>` and `.../users$/<u>`,
    `.../volume<N>/homes/<u>`, `.../share/homes/<u>`,
    `.../webdav/home/<u>`, `.../c$/Users/<u>`, and any share before the home for
    `smb`, `cifs`, `nfs`, `afp` and `file` URLs (`smb://<nas>/data/home/<u>`)
    (a lowercase `/users/` in a URL is a REST path and is kept);
  - percent-encoded: a token with `%XX` escapes is decoded (up to three times,
    for double encoding) and replaced whole when the decoded form holds any of
    the spellings above (`C%3A%5CUsers%5C<u>`, `%2Fhome%2F<u>`).
  Not covered: a home named without a `.`/`..` segment or root (`home/<u>`,
  `Users\<u>`), other nested layouts (`/data/homes/<u>`, `/mnt/wsl/home/<u>`,
  `D:\Backups\Users\<u>`, `C:\tools\msys64\home\<u>`).
- The host of any other UNC path (`\\<host>\share` keeps its share name).

Left intact (tested as negative cases): ISO dates and times (with `T`, `Z` or
offsets), other date spellings, version strings, ports (`:8443`, `port 5000`),
claim and commit ids, issue numbers, durations and ranges (`360-610 ms`,
`1.0-1.25 s`, `1200-1500`, `took 12 1200-1300`), number runs that are not 10
national digits (`id 2024-0001-2345`, `(2026) 123-4567`), signed numbers
(`+0.12345678`), counts with thousands separators, UUIDs and hashes,
public IPs and loopback, public URLs, REST paths, ordinary identifiers and
apostrophes in prose. Each field
is capped at 1,200 characters after redaction. Very long text is cut at a word
boundary before it is scanned, so no value is ever sent cut in half; a run with
no whitespace to cut at is replaced by `[REDACTED:truncated_value]`. If a credential still shows up in
any decoded form (base64, hex escapes, look-alike characters, URL encoding), the
request is **not sent** and the decision is logged as `egress_blocked`, without
the text.

Treat what is sent as retained by the provider: standard TypeSafe plans do not
offer zero retention. If that is not acceptable for a project, keep its surfaces
`off`.

## Inspecting the ledger

```python
from memorymaster.decisions.config import DecisionConfig
from memorymaster.decisions.ledger import DecisionLedger
from memorymaster.decisions.metrics import compute_metrics, to_json
from memorymaster.decisions.export import export_jsonl

ledger = DecisionLedger(DecisionConfig.from_env().decisions_db)
print(to_json(compute_metrics(ledger, daily_usd_cap=2.0)))   # health, calibration, drift
export_jsonl(ledger, "decisions.jsonl")                       # one row per decided item
```

Metrics cover volume, live share, fallbacks by reason, transport latency and
engine-time percentiles, tokens and cost per day against the cap, breaker opens, blocked egress,
agreement with the legacy action, exposure-to-use rates for Jev-driven, legacy
and explored actions, reliability bins with ECE and Brier score, and weekly
drift (PSI) of answer distributions.
