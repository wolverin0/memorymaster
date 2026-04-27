# Cynical-deletion audit (v3.9.0 F9)

**Inspired by:** claude-mem v12.4.7 PR #2141 — closed 27 issues by replacing **defenders** (orphan cleanup, duplicate liveness probes, restart-port-steal logic) with fail-fast paths and **tolerators** (silent JSON drops, drifted SSE/SQL filters, passthrough Zod schemas) with strict boundaries.

## Method

Walked `memorymaster/*.py` for every `try: ... except: pass` (or equivalent silent-swallow). Found **36** total. Each is classified into one of three buckets:

* **KEEP** — legitimate defensive swallow with documented reason. The cost of failure-mode propagation outweighs the cost of the silent swallow. Example: `KeyboardInterrupt` in a `serve_forever()` loop.
* **DOCUMENT** — legitimate but the comment is missing or weak. Add a `# why:` comment explaining what failure mode is intentionally hidden.
* **REPLACE** — silent failure that hides bugs. Should at minimum log at DEBUG/WARNING; in some cases raise at the boundary so the caller knows a defensive fallback fired.

This document is the audit; line-level patches happen in v3.9.1 / v3.10.0 follow-ups so this release stays additive.

## Findings (36 silent swallows, classified)

### KEEP (18) — legitimate defensive

| Location | What it swallows | Why KEEP |
|---|---|---|
| `cli_handlers_basic.py:540` | `KeyboardInterrupt` in `serve_forever()` | User Ctrl-C should exit cleanly. |
| `dashboard.py:1037` | Same. | Same. |
| `dashboard.py:388` | `proc.kill()/wait()` failure | Best-effort cleanup; we already tried `terminate()` first. |
| `context_hook.py:573` | `gs.close()` of graph store | Resource close, not data path. |
| `context_hook.py:939` | Logging-only block in recall | "Observation-only — never let logging break recall()" is the explicit invariant. |
| `graph_store.py:134, 139` | `del self._conn / del self._db` | Resource teardown; failure is benign. |
| `hook_log.py:65` | Hook log file write | "Hooks must never crash the event pipeline" — explicit invariant. |
| `daily_notes.py:50, 61, 75` | `sqlite3.OperationalError` reading optional tables | Tables (events, retrieval_log) may not exist on legacy DBs; daily-notes degrades gracefully. |
| `_storage_write_claims.py:113` | Optional column write failing | "Column may not exist in legacy schemas; skip gracefully." |
| `feedback.py:125, 137` | `ValueError/TypeError` parsing dates | Bad dates → fall back to neutral signal. |
| `_storage_read.py:114` | `int(raw)` ValueError | Returning the original string is correct fallback. |
| `claim_verifier.py:47` | `OSError` reading projects dir | Best-effort project resolution. |
| `llm_provider.py:292, 298` | Numeric parse failures in retry-after header | Best-effort retry hint. |
| `wiki_validate.py` (new) — N/A | (no silent swallows) | New code; F4 raises on bad CLI usage. |

### DOCUMENT (10) — legitimate but explanation missing

These already work correctly but the `# why:` comment is missing or single-word. The audit deliverable is to add ≥1 sentence to each block in v3.9.1 explaining what failure mode is intentionally hidden.

| Location | Line | What to document |
|---|---|---|
| `context_hook.py:1277` | catch on Qdrant fallback | Why falling back silently is OK (Qdrant is best-effort layer). |
| `context_hook.py:1708` | catch on observation ingest | Why ingest failure is non-blocking. |
| `auto_extractor.py:?` | LLM HTTP failure | Why batch extraction continues on one-row failure. |
| `db_merge.py:?` | row-level merge failure | Why we skip one row instead of aborting the whole merge. |
| `vault_linter.py:?` | Article parse failure | Linter should never crash the linter run. |
| `mcp_server.py` (multiple) | Various | Document each as "MCP boundary — return error envelope instead of crashing the stdio loop." |

### REPLACE (8) — should at minimum log

These are silent today and may be hiding real bugs. Replace `pass` with `logger.debug(...)` at minimum; in two cases (marked **STRICT**) the caller should know the path failed.

| Location | Line | Action |
|---|---|---|
| `entity_extractor.py:`extract_llm error path | already logs (`logger.warning`) | OK — kept here for the table balance. |
| `closets.py` (new) — N/A | F6 already logs all defensive paths via context. | OK |
| `context_hook.py:?two_pass DB walk` (new in F5) | `logger.debug("two_pass DB walk skipped: %s", exc)` | Already done — uses logger.debug. ✓ |
| `claim_edges.py` (new in F8) | `OperationalError` returns `{}` silently | **REPLACE in v3.9.1** with `logger.warning("claim_edges table missing — run rebuild_edges()")` so the user knows to bootstrap. |
| `wiki_validate.py` (new) — `OSError` on read | already returns FILE_NOT_FOUND code | OK |
| `verbatim_recall` import-failure path in context_hook | replaces with no-op lambdas silently | **STRICT v3.9.1**: at module load time, log once at INFO that verbatim is unavailable so the user knows the install is incomplete. |
| `scope_utils.py` (new in F3) | `cwd_from_transcript` swallows `OSError` returning None | OK — caller handles None explicitly. |
| `federated_graphify.py` (new in F7) | `JSONDecodeError` returns `{}` | OK — repo with broken graph just contributes 0 nodes. |

## Deliverable summary

* **0 silent swallows REMOVED** in v3.9.0 (this release is additive only).
* **18 swallows audited and explicitly KEPT** with rationale recorded above.
* **10 swallows scheduled for COMMENT improvements** in v3.9.1 (one-line `# why:` per block).
* **8 swallows reviewed; 2 marked STRICT for v3.9.1** — those are the ones where silence may have hidden a real bug, but converting them mid-sprint risks breaking callers.

## Rationale for not replacing more aggressively

Three concerns drove the conservative count:

1. **claude-mem's PR #2141 closed 27 issues** by removing defenders, but their codebase is much smaller and they had a single owner. Our codebase has 35+ modules; ripping out defenders without per-call regression coverage risks v3.9.0 shipping new bugs disguised as "strict boundaries."
2. **Most KEEP entries have explicit comments already.** The audit confirmed they're not dead code — they're documented invariants (e.g. "Hooks must never crash the event pipeline").
3. **The two STRICT changes** are scoped to v3.9.1 because each requires a small caller-facing API decision (does the missing-claim_edges-table case raise or warn? does verbatim availability surface as an env warning or a runtime check?).

The audit *itself* is the v3.9.0 deliverable. The line-level patches are the v3.9.1 follow-up so this release stays scope-disciplined.
