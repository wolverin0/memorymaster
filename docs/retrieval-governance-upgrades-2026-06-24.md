# Retrieval & Governance Upgrades — implementation plan (2026-06-24)

**Source:** the 2026-06-24 re-survey (`artifacts/steal-from-others-2026-06-24.md`, `CREDITS.md`, claim `mm-e460`).
**Goal:** land the prioritized steal candidates as measured, tested changes — retrieval quality first, then governance/correctness fixes, then new tools, then positioning.

## Ground rules (read first)
- **Edit the real subpackage files, not the deprecated top-level shims.** Package was restructured into `recall/`, `knowledge/`, `core/`, `stores/`, `surfaces/`, `govern/`; `memorymaster/*.py` are `sys.modules` aliases.
- **Run `gitnexus_impact({target, direction:"upstream"})` before editing any symbol** (repo rule) and `gitnexus_detect_changes()` before each commit. Warn on HIGH/CRITICAL.
- **Storage parity:** any schema/store change → `stores/_storage_write_claims.py` **and** `postgres_store.py` **and** tests (boundary rule).
- **Sensitivity filter runs on every ingest path** — new ingest tools included.
- **Measure retrieval changes on the LongMemEval-S harness** (`benchmark/longmemeval_s_qa.json` + the recall_analysis path). Record R@5 / MRR numbers in the table below. No regression vs the `linear` baseline is the gate; lift is the win.
- Atomic commits, conventional-commit messages, one logical change each. Branch off `main` (e.g. `feat/retrieval-governance-upgrades`).

---

## Phase 1 — Retrieval quality (the measured wins)

### 1.1 RRF: validate → promote to default  ⭐ (convergent signal: gbrain + GitNexus)
- **State:** EXISTS but not default. `recall/recall_fusion.py:rrf_fuse` (48-79); dispatch via `MEMORYMASTER_RECALL_FUSION = linear|rrf|auto` in `recall/context_hook.py` (~1981-2031). Linear combiner = `_relevance` (1902-1972).
- **Do:** A/B `linear` vs `rrf` vs `auto` on the harness; add a unit test for `rrf_fuse` (ordering, ties, k-param); if `rrf`/`auto` ≥ baseline, flip the default + document the env flag.
- **Acceptance:** [x] `rrf_fuse` unit test passes (`tests/test_recall_fusion.py`+`test_rrf_auto_gate.py`, 23 tests green)  [x] harness table filled (below)  [x] **DECISION: linear STAYS default** — RRF measurably *regresses* on our harness (953 GT prompts: precision −29%, MAP −49%, hit −20%); `auto` safely falls back to linear. Root cause: the harness runs `skip_qdrant=True` (no vector stream) so RRF fuses by rank only and discards the score-magnitude MM's calibrated linear blend exploits; the external gbrain/GitNexus "RRF wins" signal assumed a vector-inclusive fusion we can't reproduce here. RRF kept available behind `MEMORYMASTER_RECALL_FUSION=rrf|auto` for the vector-on case.  [x] env flag already documented in `context_hook.py` (1975-1990).
  - **No code change** — RRF already shipped + tested; the work was measurement, and the measurement says don't promote it.

### 1.2 Rerank in the per-prompt recall path
- **State:** `recall/llm_rerank.py:rerank_with_llm` exists but is wired ONLY in `core/service.py:query_for_context` (1096-1099, gated by `_llm_rerank_enabled`). The recall hook (`context_hook.recall`) does NOT rerank.
- **Do:** add an optional rerank pass to the recall-hook path behind `MEMORYMASTER_RECALL_RERANK` (default **off**); reuse `rerank_with_llm`, and evaluate adding a true cross-encoder option (bge-reranker / ZeroEntropy) as an alternative backend. Over-fetch candidates before rerank (mirror service.py 1037/1066).
- **Acceptance:** [ ] flag toggles rerank in the hook path  [ ] default off  [ ] harness shows no regression (ideally lift) with it on  [ ] test for the gate.

### 1.3 Intent-aware ranking
- **State:** `recall/query_classifier.py:classify_query` returns `{query_type, recommended_mode}` but does NOT feed ranking weights (only the RRF auto-gate per-type threshold uses type).
- **Do:** wire classifier output → weight/profile selection (entity→boost graph stream, temporal→boost freshness, event→boost recency) via `core/service.py:_retrieval_profile_weights`. Keep it deterministic.
- **Acceptance:** [ ] test: a temporal vs entity query produces different weight profiles  [ ] harness no-regression.

---

## Phase 2 — Governance / correctness fixes (small, high-value)

### 2.1 Bitemporal write-time guard (MemPalace)
- **State:** NO `valid_until < valid_from` guard anywhere. Fields set in `stores/_storage_write_claims.py:create_claim` (103-104); ingest in `core/service.py:ingest` (413-574).
- **Do:** reject inverted intervals + ISO-8601 sanitize `event_time`/`valid_from`/`valid_until` at ingest (raise a clear error, before `create_claim`). Mirror in Postgres path.
- **Acceptance:** [x] test: inverted interval rejected with a clear error (`tests/test_bitemporal_guard.py`, 10 tests green)  [x] malformed-ISO rejected + valid passes  [x] SQLite + Postgres parity — guard lives in `MemoryService.ingest` (backend-agnostic; `PostgresStore(SQLiteStore)` ingests through the same path); 47 passed / 39 PG-skipped on the temporal-touching suites, no regression.
  - **Implemented:** `core/models.py:validate_temporal_fields` (+ `_parse_iso_strict`) called from `core/service.py:ingest` after the empty-text check. Scope note: only rejects when BOTH bounds are explicitly passed + inverted (the clear bug); the auto-populate-`now` edge case is left as-is to preserve existing `valid_until`-only behavior (`test_integration_workflows.py`).

### 2.2 Fail-loud LLM CLI resolver (claude-mem "parseable-response = only success")
- **State:** `core/llm_provider.py:_call_claude_cli` (293-350) returns `""` on timeout/OSError/non-zero exit (336-349) — empty failure is indistinguishable from a legit empty response (silent data loss).
- **Do:** capability-probe the resolved binary (`--version`, cache result), and make failure DISTINCT from empty (raise/return a typed error, log loudly) so callers don't treat a dead CLI as "no memory". Don't mask non-zero exits.
- **Acceptance:** [ ] test: a failed CLI call is distinguishable from a successful empty response  [ ] stale/missing binary fails loud, not silent.

---

## Phase 3 — New structure & tools (bigger)

### 3.1 Community detection on the entity graph (graphify)
- **State:** entity graph in `knowledge/entity_graph.py` (`EntityGraph`, entity_edges) + `recall/graph_store.py:claims_for_entities_with_distance`. **networkx is NOT a declared dep** (only an optional fallback). GRAPH stream proven FLAT in v3.6.
- **Do:** add `networkx` + `leidenalg`/`python-igraph` as real deps; compute Leiden (Louvain fallback) communities over entity_edges with **stable size-ranked IDs** (`remap_communities_to_previous` pattern) so wiki articles don't churn; expose counts via `entity_stats`; optionally boost recall for claims whose entities share a community. Keep it opt-in if the dep is heavy.
- **Acceptance:** [ ] communities computed  [ ] IDs stable across two runs (test)  [ ] entity recall unaffected when disabled  [ ] dep added to pyproject.

### 3.2 MCP tools: `delete_by_source` + `checkpoint` (MemPalace)
- **State:** no hard-delete/purge tool or `DELETE FROM claims` anywhere; `ingest_claim` (surfaces/mcp_server.py 462-511) already has `intake_batch_id/max` batch params.
- **Do:** (a) `delete_by_source(source, dry_run=True)` → new `store.delete_by_source(...)` in `stores/_storage_write_claims.py` + Postgres parity, **dry-run default** (lists what would go), for eval/backfill-pollution cleanup. (b) `checkpoint(claims=[...])` batch-ingest tool modeled on `ingest_claim`, one round-trip for N claims, **through the sensitivity filter + auto-citation**.
- **Acceptance:** [ ] `delete_by_source` dry-run lists, real-run deletes, both backends  [ ] `checkpoint` ingests N in one call, filter enforced  [ ] tests for both.

---

## Phase 4 — Positioning (docs)

### 4.1 Reposition messaging around governance
- **Do:** update README "How it's different" + mission to lead with **governance / curation-over-accumulation** (the survey's verdict: the vector-store strawman is dead; the field closed the retrieval gap; our wedge is lifecycle+steward+citations+conflict). Keep consistent with `CREDITS.md`.
- **Acceptance:** [ ] README + CREDITS consistent; no over-claiming on retrieval.

---

## Harness results table (fill during Phase 1)

Metrics from `scripts/eval_recall_precision_at_5.py` on `real-prompts-1000-top50.jsonl` (953 ground-truth-labeled prompts, live 4.6GB DB, `skip_qdrant=True`). R@5≈precision@5, MRR≈MAP@5.

| Config | precision@5 | MAP@5 | hit@5 | Δ vs linear | Notes |
|---|---|---|---|---|---|
| **linear (baseline, SHIPPED)** | **0.072** | **0.164** | **0.250** | — | current default — kept |
| rrf | 0.051 | 0.083 | 0.201 | −29% / −49% / −20% | regresses; rank-only fusion loses score magnitude w/o vector |
| auto | 0.072 | 0.164 | 0.250 | 0 / 0 / 0 | gate falls back to linear on these prompts — safe |
| rrf + rerank | _(deferred — see 1.2)_ | | | | |
| intent weights | _(see 1.3)_ | | | | |

## Out of scope / non-goals
- Native code-structure memory (gbrain Cathedral / codebase-memory-mcp / GitNexus call-graphs) — MM delegates code-intel to GitNexus.
- Server/Postgres-platform pivot (cognee/Zep) — MM stays single-file SQLite first.
- mem0 ADD-only model — it's the negation of the steward.

## Definition of done (the /goal exit gate)
1. Every Acceptance checkbox above is checked, OR explicitly deferred in this file with a one-line reason.
2. `python -m pytest tests/ -q --tb=short` is GREEN (no new failures vs baseline count).
3. `ruff check memorymaster/` is clean.
4. The harness table is filled and the shipped retrieval config shows **R@5 and MRR ≥ linear baseline** (no regression).
5. `python -m memorymaster --db memorymaster.db run-cycle` runs without crash.
6. Postgres parity verified for 2.1 + 3.2 (schema/store touches).
7. Atomic commits per phase; a single PR opened against `main` summarizing the deltas + the harness numbers.
