# Campaigns J, L, M: graph observations, profile, dashboard, recovery

Read-only static inspection at HEAD `31c9492` (2026-10-04). Nothing in this file was measured.

## J. Graph observations and compiled profile (priority 1)

Live fact, from the 2026-10-04 review: every Dreaming run enqueues about 150 `observation_discover` jobs, and all of them end `no_supports`. Zero observations have been emitted since 2026-08-31. Only 3 observations exist, with 63 supports.

### Why discovery never finds supports

The filters below are confirmed by code. Which of them excludes everything is a hypothesis until the funnel diagnostic below runs.

1. **One job per scope, not per claim.** `dreaming/worker.py:359-371` (`_observation_scope_pairs`) selects distinct (scope, tenant) pairs of confirmed claims. `worker.py:418-425` queues one `discover` job per pair per UTC hour. The roughly 150 jobs per run are therefore about 150 scope pairs, not 150 findings.
2. **Processing.** `graph_observation_engine.py:186-234`: `process_discovery` calls `load_active_supports` and then `discover_components`. The `no_supports` outcome comes from `NO_ELIGIBLE_SUPPORTS` (`:56`).
3. **What that diagnostic means.** It is emitted only at `graph_observations.py:252-253`, when the support query returns zero rows for the scope.
4. **The support query.** `graph_observation_repository.py:347-380` starts from `entity_edge_supports` and **inner joins** `claims`, `claim_evidence_links`, `evidence_items` and `source_items`. A row survives only when:
   - the claim is confirmed, not sensitive, not observer-authored, and not an observation, skill or summary;
   - the source is not retired;
   - the source and evidence sensitivity is `none`;
   - the support's `ontology_version` equals the current one;
   - the relation is in the current ontology.

   A claim with no capture-lineage evidence link is dropped entirely.
5. **Who writes `entity_edge_supports`.** In production only `knowledge/entity_graph.py:628-676` (`_upsert_edge`) does, reached from `extract_and_link` (`:272-319`) after an LLM call and ontology validation. The other writer, `capture/repository.py:326`, is called only by `public/demo.py:124`.
6. **What triggers extraction.** The capture worker's `extract_graph` stage (`capture/worker.py:171-178`) runs `knowledge/graph_extraction.py:43-68`. Jobs are queued by `public/v1.py:585-599` from `capture/repository.py:531-581` (`due_confirmed_graph_claims`). That query itself requires evidence links and non-retired sources.
7. **Extraction refuses claims too.** `entity_graph.py:182-220` rejects a claim when it is not confirmed, is sensitive, has no evidence (`claim_evidence_missing`), or has retired or sensitive evidence. `:222-251` rejects relations or entity types outside the ontology.
8. **The capture drain is bounded.** `run_capture_worker(limit=25)` runs per invocation (`surfaces/scheduled_task.py:79-93`).

### Ranked hypotheses

- **(a)** Most confirmed claims have no `claim_evidence_links` or `source_items`. MCP and hook ingest create citations but not capture-lineage evidence, so they are dropped at steps 4 and 6.
- **(b)** The `extract_graph` queue is blocked or retrying. Live, 95 capture jobs are permanently blocked; their stage and error codes have not been read yet.
- **(c)** Ontology version drift: old supports are excluded at `:374`.
- **(d)** A component needs at least 3 claims, 2 evidence items and 2 signatures (`graph_observations.py:222`). This case would show up as `no_components`, not `no_supports`.

### Profile

- **Coverage per token is not measured anywhere.** `renderer.py:80-96` fills greedily up to `token_budget` (1400) or `max_facts` (60) and reports only `tokens_used`. The operational review added a count of omitted facts on 2026-10-02.
- **No deterministic contradiction detector.** The reducer's `replace` supersedes (`repository.py:691-703`).
- **Support independence.** `repository.py:74-96` groups supports by session lineage, and claims with no session share `claim:unlinked`. `:526-548` requires 2 independent sessions.
- **Expiry.** Only `preference` facts expire, after 90 days (`repository.py:772-782`).

### Campaign definition

- **Falsifiable hypothesis:** if evidence-lineage loss (a) is the cause, then counting claims with and without evidence links per scope explains every `no_supports` scope.
- **Primary metric:** scopes whose `load_active_supports` returns at least 1 row, and observations emitted per week.
- **Secondary metrics:** `outcome_counts` (`graph_observation_repository.py:601`), `extract_graph` job error codes, and profile `tokens_used` against omitted facts.
- **Constraints:** sensitivity gates stay; no disabled feature gets enabled to pass; use a disposable DB or a read-only diagnostic.
- **Reuse:** `outcome_counts`, `public/demo.py` (it builds a supported graph), and the graph and profile tests.
- **Missing:** a read-only per-scope funnel that counts the rows lost at each join of step 4, and a profile coverage metric.
- **Cost:** medium. **Priority:** 1, because it is the only campaign tied to a live, quantified failure (ROADMAP item 8).

## L. Dashboard and diagnostics (priority 3)

**Evidence.**
- `tests/test_dashboard_latency.py` does **not** measure page load or HTTP latency. It tests `/metrics/validation-latency`, the time from claim creation to first validation, backed by `dashboard.py:123-173`. The static review's premise is partly wrong.
- There is no instrumentation for HTTP p95, payload size or SQL count.
- `_handle_observability` (`dashboard.py:1140-1180`) tails 1,500 log rows, fetches 600 events, builds a 250-item review queue and counts with `Counter`.
- `conflicts_payload` (`dashboard_read_models.py:61-85`) over-fetches `max(limit*12, 200)` rows.
- `_validation_latency_metric` loads every validated claim and sorts it in Python.

**Hypothesis.** `/api/observability` dominates refresh cost. Moving its counts to SQL `GROUP BY` would leave the returned payload unchanged.

**Metrics and constraints.**
- **Primary metric:** server-side p95 wall time for each `/api/*` route on a seeded fixture.
- **Secondary metrics:** response bytes, SQL statement count, peak RSS.
- **Constraints:** an identical JSON payload (checked by a snapshot test), preserved scope authorization, and read-only access.

**Reuse.** The `_running_server` fixture and `tests/test_dashboard.py`.

**Cost.** Low to medium. **Priority:** 3, because there is no known live failure.

## M. Recovery and storage (priority 2)

Status: the memory figures below are a **hypothesis**, inferred from code, never measured.

**Evidence.**
- **Backup, `govern/recovery.py:71-107`:**
  - `snapshot.backup` writes a full plaintext copy to a temp dir.
  - `read_bytes()` (`:89`) then loads the whole database into RAM.
  - Fernet `encrypt` (`:90`) is not streamable and produces base64 about 1.33 times the plaintext size. Plaintext and ciphertext are held together (`:100-101`).
- **Restore drill, `:110-139`:** ciphertext, plaintext and Fernet transients are all in RAM at once.
- **SQLite backup/restore is fine.** The backup API used in `stores/snapshot.py:132-149` and `:251-274` streams pages.

**What a streaming alternative needs.**
- Chunked authenticated encryption, such as AES-GCM or ChaCha20-Poly1305 per chunk, with a counter nonce and a final-chunk flag to detect truncation.
- Incremental hashes.
- A manifest `schema_version` bump, while keeping the v1 Fernet read path.
- Atomic `.part` files.
- An explicit format decision before any of this.

**Hypothesis.** Peak RSS grows at least linearly with database size, and a chunked format makes it roughly constant.

**Metrics and constraints.**
- **Primary metric:** peak RSS against database size, on synthetic databases of 50, 200 and 800 MB.
- **Secondary metrics:** time to restore, output size, `integrity_check`.
- **Constraints:** round-trip checksums, tamper and truncation detection, v1 backups stay restorable, and the key is never logged.

**Reuse.** `tests/test_recovery_operations.py`.

**Missing.** A memory-scaling benchmark and a truncation test.

**Cost.** Medium. **Priority:** 2.
