# v3.20.0 backlog — Phase 1 + Phase 2 pendings

**Status as of 2026-05-17 post-v3.19.0.** Phase 0 hardening (H1-H4) shipped
at commit `39ac4ff`. Everything below is ranked and bite-sized so each item
can be a single bounded `/goal` session. Pick whichever is the right size
for your wall-clock budget.

Each item below ends with a **`/goal`** block that's ready to paste verbatim.

---

## 1. v3.20.0-S1 — Versioned migrations framework  *(recommended next)*

**Why now.** Schema today evolves via opportunistic `ALTER TABLE` with
try/except in `_storage_schema.py`. No version tracking, no rollback,
silent drift between SQLite and Postgres. This is the gating block for
v3.20.0 per `docs/ROADMAP.md`.

**Scope.**
- New `memorymaster/migrations/` package with versioned `.py` migration
  files (`0001_initial.py`, `0002_*.py`, ...).
- New `schema_versions` table (id, version, applied_at, checksum) on both
  SQLite and Postgres backends.
- `MigrationRunner` class: discover, sort, apply pending; idempotent and
  re-entrant.
- CLI: `python -m memorymaster migrate` (auto-apply pending) and `--list`
  / `--status` subcommands.
- `MemoryService.init_db()` and `store_factory.create_store()` call
  the runner on startup; existing schema becomes the baseline `0001`.

**Tests (`tests/test_migrations.py`):**
- New-DB run applies all migrations in order, stamps versions, idempotent
  re-run is a no-op.
- Mid-version DB applies only the pending tail.
- Checksum mismatch on an applied version raises `MigrationDriftError`.
- SQLite and Postgres backends produce identical schema-version output
  (parametrized fixture).

**Verifiable end-state.** PR via `omni/v320-s1-migrations` merged
`--squash --admin` with full pytest green. `python -m memorymaster
migrate --status` on a fresh DB shows all versions applied.

**`/goal` to paste:**

```
/goal Ship v3.20.0-S1 (versioned migrations framework) per docs/v320-backlog.md and docs/ROADMAP.md Phase 1. Build memorymaster/migrations/ package with versioned migration files starting at 0001_initial.py (snapshot of current schema as the baseline). Add schema_versions(id, version, applied_at, checksum) table to both schema.sql (SQLite) and schema_postgres.sql (Postgres). New MigrationRunner class that discovers files, sorts by version, applies pending only, computes sha256 checksum per file, raises MigrationDriftError on checksum mismatch. Wire runner into MemoryService.init_db and store_factory.create_store as a startup step. New CLI: python -m memorymaster migrate (auto-apply), --list (show known versions), --status (show applied vs pending). PR via omni/v320-s1-migrations branch merged --squash --admin --delete-branch with full pytest green. Tests in tests/test_migrations.py: new-DB applies all, idempotent re-run, mid-version DB applies tail, checksum-drift raises, SQLite/Postgres parametrized backend produces identical version output. Reuse session patterns: commit-guard requires omni/* branch for pyproject.toml (mm-fc1b~2); admin merge tolerates worktree-attached-branch delete warning (mm-8b8d); single-chokepoint pattern for cross-cutting policy (mm-2e9b~2). Goal satisfied when the PR is merged and tests still pass on main.
```

---

## 2. v3.20.0-S2 — SQLite/Postgres parity gate  *(depends on S1)*

**Why.** Once S1 lands, every schema change SHOULD apply identically to
both backends. S2 adds the CI/test gate that proves it: a parametrized
test suite that runs the same scenarios against both stores and asserts
identical observable behaviour.

**Scope.**
- New parametrized fixture `parametrize_backends` in `tests/conftest.py`
  that yields a fresh SQLite store and a fresh Postgres store (when
  `TEST_POSTGRES_DSN` is set; xfail when unset so dev machines pass).
- `tests/test_backend_parity.py` covering: ingest → list, query,
  status transitions, citations, events, retrieval rank order on a
  fixed corpus.
- CI matrix update: Postgres job runs the parity tests with a real DSN.

**Verifiable end-state.** PR via `omni/v320-s2-parity-gate` merged.
`TEST_POSTGRES_DSN=... pytest tests/test_backend_parity.py` passes both
backends; CI shows the matrix expanded.

**`/goal` to paste:**

```
/goal Ship v3.20.0-S2 (SQLite/Postgres parity gate) per docs/v320-backlog.md. Add parametrize_backends fixture in tests/conftest.py that yields a fresh SQLite store and (if TEST_POSTGRES_DSN is set) a fresh Postgres store; xfail/skip the Postgres parametrization when DSN is unset so dev machines pass. New tests/test_backend_parity.py with parametrized scenarios for: ingest then list, query with retrieval rank order on a fixed corpus, status transitions (candidate → confirmed → archived), citations attached and round-trip, events written on each mutation. Assert observable equivalence between backends (claim count, status, citation count, retrieval order). Update .github/workflows/ci.yml to add a Postgres job that sets TEST_POSTGRES_DSN against a service container. PR via omni/v320-s2-parity-gate branch merged --squash --admin --delete-branch with full pytest green. Depends on S1 (versioned migrations) being merged first. Goal satisfied when the PR is merged and tests still pass on main.
```

---

## 3. v3.20.0-release — Cut the v3.20.0 release tag  *(after S1+S2)*

**Why.** Bundle S1 + S2 into a single tagged release with CHANGELOG +
GitHub release notes mirroring the v3.19.0 format.

**Verifiable end-state.** Tag `v3.20.0` on `origin` + GitHub release
published. Follows the same pattern as PR #117 (v3.19.0).

**`/goal` to paste:**

```
/goal Cut v3.20.0 release tag after S1+S2 are both merged to main. Cut omni/release-v3.20.0 branch, bump pyproject.toml from 3.19.0 to 3.20.0, add CHANGELOG entry covering S1 (versioned migrations framework) + S2 (SQLite/Postgres parity gate) with env-var reference table (any new TEST_POSTGRES_DSN or MEMORYMASTER_MIGRATION_* vars), PR + merge --admin, tag v3.20.0 at the merge commit, push tag, gh release create v3.20.0 with notes. Reuse session patterns: commit-guard requires omni/* branch for pyproject.toml (mm-fc1b~2); admin merge tolerates worktree-attached-branch delete warning (mm-8b8d). Goal satisfied only when tag v3.20.0 exists on origin and GitHub release v3.20.0 is published.
```

---

## 4. A1 — Full LongMemEval-S QA-accuracy publication run  *(orthogonal, mostly wait)*

**Why.** Mechanism shipped in v3.18.0 (PR #109). v3.19.0-H1 budget caps
make it safer to dispatch unattended. This is the first published full
QA-accuracy number for any of the LongMemEval-S memory systems — pure
credibility win, no risk.

**Scope.** No code change. One dispatch, capture the result, update
`docs/longmemeval-results.md` + `README.md`, commit + push.

**Verifiable end-state.** `docs/longmemeval-results.md` has a QA-accuracy
section with the number; commit pushed to `main` via PR.

**`/goal` to paste:**

```
/goal Run the A1 full LongMemEval-S QA-accuracy publication bench and publish the result. Set MEMORYMASTER_LLM_RERANK=0, MEMORYMASTER_LLM_MODEL=claude-sonnet-4-5, PYTHONUNBUFFERED=1, MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE=1500 (safety cap, ~50% headroom over 500q×2calls projection). Dispatch python -u tests/bench_longmemeval.py --full --judge claude_cli --judge-pacing-seconds 0 --qa-max-seconds 30000 in background; project ~7h wall time. On completion: extract RESULT_QA_ACCURACY + per-question-type breakdown from benchmark/longmemeval_s_results.json. Update docs/longmemeval-results.md with a QA-accuracy section. Update README.md to mention the published QA number. PR via omni/a1-qa-publication branch merged --squash --admin. Goal satisfied when the bench completes, docs are updated, and the PR is merged.
```

---

## 5. v3.21.0-A1 — Split postgres_store.py (2591 LOC)  *(architectural, multi-session)*

**Why.** Per ROADMAP, postgres_store.py at 2591 LOC is the worst offender.
Split by workflow (read paths, write paths, lifecycle, retrieval, events)
into a `memorymaster/stores/postgres/` package. Tests stay green.

**Scope.** Multi-session work — too large for one bounded `/goal`.
Suggest doing as a sequence of smaller PRs:
- A1a: extract `postgres/connection.py` (pool, retries)
- A1b: extract `postgres/claims_reads.py`
- A1c: extract `postgres/claims_writes.py`
- A1d: extract `postgres/events.py`
- A1e: extract `postgres/retrieval.py`

**Defer.** Don't start until S1+S2 ship — migrations make the split
safer (schema is versioned, drift can't sneak in).

---

## 6. v3.21.0-D1 — Conflict-resolution UI for the dashboard  *(differentiator)*

**Why.** Per ROADMAP, this converts governance from "exists in code" to
"exists in workflow." None of MemPalace/agentmemory/mem0/Letta/Zep have
this — it's the user-facing differentiator that R@5 doesn't capture.

**Scope.** New `/conflicts` dashboard view that lists conflicted-pair
claims side-by-side, with one-click `pin` / `redact` / `supersede`
buttons calling the existing MCP tools.

**Defer.** Wait until S1+S2+release ship — don't mix governance-feature
work with storage-discipline work in the same release.

---

## 7. v3.21.0-R1 — Rule-shaped claims with auto-correction-extraction  *(borrows from claude-smart)*

**Why.** MM's existing claims are *descriptive* — "the API uses Y". They
work great for facts/decisions/constraints, but they don't capture the
prescriptive shape Claude needs to actually *change behaviour next time*:
"when doing Z, do Y because W." `ReflexioAI/claude-smart` (256 stars,
GA 2026-04-21) ships exactly this — preferences + skills with structured
trigger/action/rationale, auto-extracted from user corrections via session
hooks. Their benchmark vs `claude-mem` claims 3× higher correction-to-rule
yield. Adopting the shape inside MM (instead of running both systems and
fighting hook conflicts) closes the gap without duplicating storage,
dashboards, or embedders.

**Scope.**

- New `claim_type="rule"` (single migration via the v3.20.0-S1 framework —
  schema-version-tracked, drift-safe).
- Structured fields on rule-typed claims: `trigger` (when this fires),
  `action` (what to do), `rationale` (why). Carried as JSON in a new
  `rule_payload` column, or three discrete columns — pick during impl.
- Auto-extraction: extend the existing `dream_bridge` LLM prompt to
  *also* scan transcripts for correction-signaled turns ("no", "instead",
  "actually", explicit `/learn`) and emit rule-shaped claims with
  trigger/action/rationale set. Reuse the existing Auto Dream pipeline —
  no new hook surface needed.
- Recall path: `context_hook` (UserPromptSubmit) gets a small change to
  surface matching rules with the same lexical/vector blend used for
  other claim types, but rendered in their prescriptive form (the rule
  is the literal text injected, no paraphrasing).
- MCP tool: `query_rules(query, limit)` for callers that want only
  rule-shaped hits, separate from `query_memory`.
- Wiki integration: rules absorb into a dedicated `rules/` section per
  scope, with the trigger/action/rationale rendered as a structured
  table.

**Why not just install claude-smart?**

- Hook conflict: claude-smart and MM both want UserPromptSubmit injection;
  double-injection bloats context.
- Storage split: claude-smart writes `~/.reflexio/` + `~/.claude-smart/`,
  MM writes its own DB. Same content captured twice.
- Dashboard collision: claude-smart serves localhost:3001; MM serves 8765.
- The genuinely new idea is the *rule shape*, not the system around it —
  cheaper to add the shape than to maintain two memory layers.

**Tests (`tests/test_rule_claims.py`):**

- Round-trip: ingest a rule-typed claim → query_rules → trigger/action/
  rationale preserved.
- Migration: existing `fact`-typed claims unaffected; new column nullable.
- Auto-extract: feed a synthetic transcript with a clear correction
  ("no, use pnpm not npm") through the dream pipeline; assert a rule
  claim was emitted with the right structured fields.
- Recall: a query that semantically matches a rule's trigger returns the
  rule with its prescriptive text intact.
- Negative: a non-correction turn does NOT produce a rule claim
  (no false positives on every "ok" or "thanks").

**Verifiable end-state.** PR via `omni/v321-r1-rule-claims` merged
`--squash --admin` with full pytest green. A canned transcript-replay
test demonstrates extracting at least 1 rule from a known-correction
turn, and `query_rules "when adding a new dep"` returns it.

**`/goal` to paste:**

```
/goal Ship v3.21.0-R1 (rule-shaped claims with auto-correction-extraction) per docs/v320-backlog.md item 7. Borrow the prescriptive rule shape from ReflexioAI/claude-smart (256-star Claude Code plugin) without installing it. Steps: (1) new claim_type="rule" via a v3.20.0-S1 versioned migration that adds a `rule_payload` JSON column to claims (nullable, NULL for existing fact-typed claims). (2) Extend dream_bridge's LLM extraction prompt to also scan transcripts for correction-signaled turns ("no", "instead", "actually", explicit /learn) and emit rule-shaped claims with structured trigger/action/rationale. (3) MCP tool `query_rules(query, limit)` separate from query_memory for callers that want only rules. (4) context_hook (UserPromptSubmit) surfaces matching rules in their prescriptive form alongside existing claim recall. Tests in tests/test_rule_claims.py: round-trip ingest+query, migration leaves fact-typed claims untouched, auto-extract from canned correction transcript emits rule with right structured fields, recall returns matching rule, non-correction turn does NOT emit rule. PR via omni/v321-r1-rule-claims branch merged --squash --admin --delete-branch with full pytest green. Reuse patterns: commit-guard requires omni/* branch for pyproject.toml (mm-fc1b~2); admin merge tolerates worktree-attached-branch delete warning (mm-8b8d); single-chokepoint pattern for cross-cutting policy (mm-2e9b~2); writing a new migration via the v3.20.0-S1 framework (mm-2a36). Goal satisfied when the PR is merged, tests pass on main, and `query_rules "when adding a new dep"` returns at least one extracted rule.
```

---

## Order of work

Sequential per ROADMAP Phase 1:

1. **S1** (versioned migrations) → ship as standalone PR ✓ *(merged 2026-05-17)*
2. **S2** (parity gate) → depends on S1
3. **release v3.20.0** → tag after S1+S2
4. **A1 publication** → orthogonal, can run any time after v3.19.0
5. **A1 module splits** → Phase 2, multi-session
6. **D1 conflict UI** → Phase 2, multi-session
7. **R1 rule-shaped claims** → Phase 2, depends on S1 (uses the migration framework)

Items 4-7 are independent of each other; pick by interest, not order.
