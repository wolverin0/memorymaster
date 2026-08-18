# Coding Conventions

**Analysis Date:** 2026-08-17
**Version analysed:** MemoryMaster 4.7.6, commit `c832df4`

These are the rules the code and its guard tests actually enforce. Where a rule is only convention (no automated check), that is stated.

## Code Style

**Formatting / linting — ruff** (`pyproject.toml:64-70`):
- `target-version = "py310"`, `line-length = 120`, rules `["E", "F", "W"]`, `E501` ignored (formatter owns line length).
- Run `ruff check memorymaster/` before finishing. Ruff is **not** a CI job — `.github/workflows/ci.yml` runs pytest only, so lint is a local discipline.

**Typing:**
- `from __future__ import annotations` at the top of every module — 224 of 305 package files; use it in new files.
- PEP 604 unions (`str | None`), `collections.abc` imports over `typing` equivalents (`Mapping`, `Sequence`, `Iterable`, `Iterator`).
- mypy is configured (`check_untyped_defs = true`) but not enforced in CI.

**Size budgets — enforced by `tests/test_architecture_budgets.py`:**

| Target | Cap | Assertion |
|---|---|---|
| `memorymaster/core/service.py` | 2450 lines | `test_architecture_budgets.py:23` |
| `memorymaster/surfaces/dashboard.py` | 1550 lines | `:24` |
| class `DashboardRequestHandler` | 720 lines | `:25` |
| `core/services/integration.py`, `surfaces/dashboard_read_models.py`, `surfaces/dashboard_commands.py` | 800 lines each | `:31` |
| every function in those three files | 50 lines | `:44` |

CONTRIBUTING.md states the general targets: functions < 50 lines, files < 800 lines, max 4 levels of nesting, prefer new objects over mutation.

## Naming Patterns

- **Modules:** `snake_case.py`, grouped by layer directory (`core/`, `stores/`, `recall/`, `govern/`, `knowledge/`, `bridges/`, `surfaces/`, `dreaming/`, `evaluation/`, `capture/`).
- **Private module split:** files that are internal decompositions of a public module get a leading underscore — `stores/_storage_read.py`, `_storage_write_claims.py`, `_storage_lifecycle.py`, `_storage_schema.py`, `_storage_shared.py`, `_storage_sources.py`, `_storage_pagination.py`.
- **Functions:** `snake_case`; module-private helpers prefixed `_` (`_structured_error`, `_resolve_db`, `_call_google`).
- **Constants:** `UPPER_SNAKE`, module-private ones `_UPPER_SNAKE` (`_SECRET_PATTERNS`, `_PROVIDERS`, `CLAIM_STATUSES`, `EVENT_TYPES`).
- **Env vars:** always `MEMORYMASTER_*` for own settings; third-party vendor names kept as-is (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `QDRANT_URL`, `DATABASE_URL`).
- **Migrations:** `NNNN_snake_case_description.py` in `memorymaster/stores/migrations/`, sequential (`0001_initial.py` … `0021_compiled_user_profile.py`).
- **Tests:** `tests/test_<subject>.py`, functions `test_<behaviour_being_asserted>` — long descriptive names are the norm (`test_gradual_size_budgets_prevent_facade_regrowth`).

## Comments and Docstrings

The dominant convention is the **WHY comment**: non-obvious code carries a block explaining the incident or constraint that produced it, often with a date or spec reference. Examples worth imitating: `tests/conftest.py:196-210` (why snapshots are redirected), `pyproject.toml:74-78` (why `packages.find` is scoped), `core/security.py:53-57` (why private IPv4 is deliberately *not* filtered at ingest).

- Module docstrings state purpose **and** design constraints; several also carry a deprecation verdict with its date and owner (`bridges/qmd_bridge.py:3-5`, `bridges/federated_graphify.py:3-7` — both `KEEP-DEPRECATED, 2026-06-10`).
- Sensitivity-filter patterns are annotated with the refresh that added them (`v2-refresh (oauth_db_row):`, `core/security.py:30-32`) and with the false positive that motivated the tightening.
- No enforced docstring style (not Google/NumPy); prose paragraphs, sometimes with a `Configuration via environment variables` section (`core/retry.py:7-15`).

## Claim Lifecycle (the core domain invariants)

**Statuses** — exactly six, `CLAIM_STATUSES` in `memorymaster/core/models.py:7`: `candidate`, `confirmed`, `stale`, `superseded`, `conflicted`, `archived`. This tuple is the single source of truth; adding one requires `models.py` + `schema.sql` CHECK + `schema_postgres.sql`.

**Tiers** — `core`, `working`, `peripheral`. Schema default is `working` (`schema.sql:26`). Not schema-constrained, so staying inside the three canonical values is a code discipline. Tier feeds recall ranking directly: `_TIER_BONUS = {"core": 0.15, "working": 0.0, "peripheral": -0.10}` (`recall/retrieval.py:141`). Set by `recompute_tiers` on the steward cycle — do not set manually.

**Visibility** — `public` (default), `private`, `sensitive` (`schema.sql:33`). A SQLite trigger ABORTs any non-`public` row that lacks a `source_agent` (`schema.sql:43-46`) — always pass `source_agent`.

**Events** — every transition writes a typed event. `EVENT_TYPES` (`models.py:18`) has 22 members; only 8 are legal for status transitions (`STATUS_TRANSITION_EVENT_TYPES`, `models.py:44`). `validate_event_type` / `validate_transition_event_type` raise `ValueError` on anything else — go through them, never a raw insert.

**Scopes** — `project:<slug>`, `user`, `team:<name>`, `global`. Slugs are canonicalised by `core/scope_utils.py:canonicalize_slug:49`, which strips copy suffixes (`-copy`, `(1)`) and channel suffixes (`-final`, `-prod`, `-dev`, `-staging`, `-qa`, `-test`) so the same project can't fragment into several scopes. Derive scope with `scope_from_cwd:75` / `scope_from_transcript:131` rather than hand-building strings.

**Bitemporality** — `event_time` (when the fact occurred), `valid_from`, `valid_until`. Convert relative dates ("Thursday") to ISO-8601 before storing.

**Supersession** — set `supersedes_claim_id` on the new claim AND `replaced_by_claim_id` on the old one; broken pairs break recall and the wiki. `ingest_claim` does not expose supersession — go through `govern/auto_resolver.py` or `govern/conflict_resolver.py`.

## Sensitivity Filter (never weaken)

`memorymaster/core/security.py` holds ~30 named patterns in `_SECRET_PATTERNS` (`security.py:17`): vendor API keys (OpenAI, Anthropic, Google, AWS, Stripe, GitHub all prefixes, Slack, Telegram), JWT/bearer, PEM private-key blocks, DB URLs with embedded passwords, password/token assignments, hex tokens with credential context, markdown/inline credential shapes, `sshpass -p`, inline `mysql -p`, prose passwords, private IP **with port**, Windows/Unix home paths that leak a username, and card PANs.

Rules:
- The filter runs at ingest on every path: `surfaces/mcp_server.py` (`_sensitive_input_error:193`), `bridges/dream_bridge.py`, `core/service.py:ingest`. Any new ingest path is default-deny until wired in.
- Scanning is **variant-aware**: base64 and hex-escape candidates are decoded and rescanned (`security.py:387-447`) so an encoded secret still trips the filter.
- **False-positive suppression is explicit, not fuzzy**: `_PLACEHOLDER_MARKERS` (`security.py:309`) recognises tutorial placeholders (`YOUR_STRIPE_KEY_HERE`, `${VAR}`, `{{ expr }}`) and `_is_low_entropy_value:337` drops obvious non-secrets.
- **Private IPv4 without a port is deliberately NOT filtered at ingest** (`security.py:53-57`) — infra claims need it. The redaction happens at *export* time in `dream_bridge.py` via `_DREAM_EXTRA_PATTERNS`. Do not "fix" this by adding it to the ingest list.
- There is no `allow_sensitive=True` free pass. `resolve_allow_sensitive_access` (`security.py:280`) raises `PermissionError` unless `MEMORYMASTER_ALLOW_SENSITIVE_BYPASS=1` (or a security-config override) is set — CI sets it explicitly (`ci.yml`, `env:` on the test step).
- Every filter change ships with a case in `tests/test_sensitivity_filter_*.py`, red-bar first. Adversarial corpora live at `tests/fixtures/sensitivity_adversarial.jsonl` and `_v2.jsonl`.
- Ingest-side filtering is the last line of defence; display-time redaction (`redact_claim_payload`, dashboard masks) is a separate layer and does not excuse a weaker ingest filter.

## Intake Policy (additive only)

`memorymaster/core/intake_policy.py` sits at the single chokepoint `MemoryService.ingest`, **after** `sanitize_claim_input` and **before** `store.create_claim`. Its contract (`intake_policy.py:4-8`): it may only RAISE the bar — reject more or attribute more — never flip a rejected claim into an accept, and never reorder or gate the sensitivity filter. Rejection raises `IntakeRejected`, a `ValueError` subclass, so existing `except ValueError` handlers surface it as a structured `VALIDATION_ERROR` with no new plumbing.

## Storage Discipline

**WAL is mandatory.** All connections go through `stores/_storage_shared.py`, which uniformly applies `PRAGMA foreign_keys = ON`, `PRAGMA journal_mode = WAL`, and `PRAGMA busy_timeout` (default 15000 ms, `_storage_shared.py:128-130`). Never open a raw `sqlite3.connect` in feature code — that is how the pre-consolidation 0/5000/30000 ms divergence happened.

**WAL discipline / spool** (`MEMORYMASTER_WAL_DISCIPLINE=1`, `core/spool.py`): high-frequency ambient writers (recall-hook access/feedback, Stop-hook verbatim, dream bridge) append JSONL envelopes instead of opening a multi-GB DB per event. Wire protocol is one line per event: `{"v":1,"op":…,"ts":…,"idempotency_key":…,"payload":{…}}`. Root is `~/.memorymaster/spool/` (override `MEMORYMASTER_SPOOL_DIR`), per-DB subdir `<db-name>-<path-hash8>/` so two DBs sharing a filename cannot drain into each other, one file per writer-process per day, lines ≤4 KB. The drainer **renames before reading**; unparseable lines go to `quarantine/`, never dropped.

**SQLite/Postgres parity.** Any schema or write-path change must land in `memorymaster/schema.sql`, `memorymaster/schema_postgres.sql`, `stores/storage.py` and `stores/postgres_store.py` together. Parity is asserted by the `parametrize_backends` fixture (`tests/conftest.py:158-174`), which runs one test body against both backends, and by `tests/test_backend_parity.py`, `test_storage_parity.py`, `test_postgres_parity.py`. Postgres policy expressions have their own contract tests (`stores/postgres_policy_contract.py`, `tests/test_postgres_policy_expression_contract.py`, `test_postgres_policy_fingerprint.py`).

**Schema changes need a migration.** Add `stores/migrations/NNNN_*.py`; the runner (`migrations/runner.py`) applies them in order. Do not mutate an already-shipped migration.

## Error Handling

- **Surfaces return structured errors, never tracebacks.** `mcp_server.py:_structured_error:126` produces `{"error": …, "code": …, "field": …}`. Codes in use: `VALIDATION_ERROR`, `INVALID_INPUT`, `MISSING_FIELD`, `INPUT_TOO_LONG`, `SENSITIVE_INPUT`, `RATE_LIMITED`, `ENTITY_GRAPH_NOT_READY`.
- **Domain errors are `ValueError` subclasses** so they funnel into the existing `except ValueError` handlers (`IntakeRejected`). Security refusals are `PermissionError` (sensitive bypass, Qdrant quarantine). Metadata rejection is `SensitiveMetadataError`.
- **Degrade, don't crash, on optional dependencies.** Optional imports are wrapped in `try/except ImportError` with a `None` sentinel (`mcp_server.py:40-42`) or a runtime probe (`_fts5_available`, `_probe_claude_cli`). Degraded results are signalled in the payload with `degraded: true` (local search, Atlas LLM extractor) rather than an exception.
- **Retries are centralised**, not inline: `core/retry.py` (exponential backoff, `MEMORYMASTER_DB_RETRIES` / `MEMORYMASTER_DB_RETRY_BASE`) plus `tenacity` for HTTP.
- **Provider failures fall back, then count.** `call_llm` (`core/llm_provider.py:704`) detects quota-shaped responses and switches to `MEMORYMASTER_LLM_FALLBACK_PROVIDER`, recording it in `get_fallback_stats()`.
- Failed side-effects record an event and preserve the row rather than aborting the pipeline (media: `media_process_failed`, `bridges/media_providers.py:7-11`).

## Environment Variable Patterns

- Read env through a helper, not `os.getenv` scattered inline: `core/config.py` for tunables (JSON overlay via `MEMORYMASTER_CONFIG_FILE`), `_env()` in `core/llm_provider.py:75` for provider settings so `ContextVar` call-scoped overrides apply.
- Booleans use a shared truthy set (`1/true/yes/on`) — `_TRUE_VALUES` in `bridges/evidence_policy.py:14`, `_as_bool` in `core/security.py:244`. Don't invent a new parse.
- **Every new feature is off by default and named after itself**: `MEMORYMASTER_WIKI_ABSORB`, `MEMORYMASTER_RECALL_GRAPH`, `MEMORYMASTER_RECALL_VERBATIM`, `MEMORYMASTER_RECALL_FUSION`, `MEMORYMASTER_PRETOOLUSE_RECALL`, `MEMORYMASTER_WAL_DISCIPLINE`.
- Unsafe operations require an explicitly-named escape hatch, never a silent default: `MEMORYMASTER_DASHBOARD_UNSAFE_BIND`, `MEMORYMASTER_ALLOW_SENSITIVE_BYPASS`, `MEMORYMASTER_TEST_POSTGRES_RLS_DISPOSABLE`.
- Secrets only ever come from env or a key file — never a literal, never a committed default.

## Module Design

- **Layer directories are the public shape**; the flat modules at `memorymaster/*.py` (e.g. `storage.py`, `llm_provider.py`, `service.py`) are **deprecated shims** that rebind `sys.modules[__name__]` to the new location (`memorymaster/storage.py:1-11`). Import from the layer path (`memorymaster.stores.storage`, `memorymaster.core.llm_provider`) in all new code. The shims carry a dated removal gate — `docs/compatibility.md` must keep the `2026-09-30` date, asserted by `tests/test_architecture_budgets.py:63`.
- **Facades stay thin by inheritance:** `MemoryService` must remain a subclass of `IntegrationService` with extracted methods living only in the parent (`test_architecture_budgets.py:46-56`).
- **Pluggable backends use `typing.Protocol`**, not ABCs — `LocalSearchProvider` (`bridges/local_search/provider.py`), `TranscriptionProvider` / `OcrProvider` (`bridges/media_processing.py`).
- `__all__` is declared in modules that define a public DTO surface (`bridges/local_search/provider.py:11`, `core/scope_utils.py:25`).
- Heavy or optional imports go **inside the function**, not at module top (`mcp_server.py:1393` imports `EverythingProvider` inside `local_search`) to keep import time and optional-dep coupling down.
- No barrel files: `__init__.py` are essentially empty; import concrete modules.

## Deprecation Convention

Do not delete a surface silently. Mark the module docstring with the census finding, the verdict, the verdict date, and the deferred decision point — `bridges/qmd_bridge.py:3-5` and `bridges/federated_graphify.py:3-7` are the reference shape (`KEEP-DEPRECATED, 2026-06-10`, wire-or-remove deferred to P5). Compatibility shims get a dated removal gate in `docs/compatibility.md`.

---

*Convention analysis: 2026-08-17*
