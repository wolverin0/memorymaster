# MemoryMaster — Remaining Roadmap (2026-04-23)

**Current main:** `5285edd` (all P0/P1/P2 work shipped this session).
**Remaining:** 2 architectural implementations (specs already landed) + 1 batch task waiting on user input.

---

## #127 — NER entity extraction at ingest (Wave 3)

**Spec:** `artifacts/spec-entity-extraction-at-ingest-2026-04-23.md`
**Why it matters:** `avg_aliases_per_entity = 1.033`. The registry is decorative. Raising it to ≥2.0 makes typed relationships actually work and fixes the #127 complaint that "every entity has degree 1".
**Files touched:** new `memorymaster/entity_extractor.py`, patches to `memorymaster/service.py`, new fixture + script + test.
**Estimate:** ~1 day (Layer 1 only), +1 day (Layer 2 LLM, optional).

### Sub-tasks (sequential within, no hidden dependencies on #129)

| # | Task | Deliverable | Acceptance |
|---|---|---|---|
| 127.1 | Build labeled eval fixture | `tests/fixtures/entity_extraction_eval.jsonl` — 100 claims with `{text, expected_entities: [...]}` | ≥5 examples per entity-kind (file, env-var, service, port, commit, tool) |
| 127.2 | Layer-1 extractor module | `memorymaster/entity_extractor.py::extract_patterns(text) -> list[Entity]` | Unit tests: recall ≥0.9 + FPR ≤0.1 per class on fixture |
| 127.3 | Service integration | Patch `service.py::MemoryService.ingest` to register extracted entities post-`resolve_or_create` | All existing ingest tests pass |
| 127.4 | Backfill script | `scripts/backfill_entity_extraction.py --dry-run / --apply` | Idempotent: re-run = 0 new rows |
| 127.5 | Run live backfill + measure | `artifacts/entity-extraction-wave3-2026-04-23.md` with before/after metrics | `avg_aliases_per_entity ≥ 2.0` |
| 127.6 | (Optional) Layer 2 LLM | `extract_llm(text)` + `MEMORYMASTER_ENTITY_LLM=1` gate | `avg_aliases ≥ 2.5` |

---

## #129 — Calibrated steward classifier

**Spec:** `artifacts/spec-steward-classifier-2026-04-23.md`
**Why it matters:** Pareto sweep showed the additive `validation_score` ceiling is 49% recall @ 1% FPR — tuning thresholds can't fix this. Need a feature-weighted model.
**Files touched:** new `memorymaster/steward_features.py`, new `memorymaster/steward_classifier.py`, patches to `memorymaster/steward.py`, new training + eval scripts.
**Estimate:** ~2 days.

### Sub-tasks

| # | Task | Deliverable | Acceptance |
|---|---|---|---|
| 129.1 | Feature extractor | `memorymaster/steward_features.py::extract_features(claim) -> dict` with 9 features from spec | Unit tests: each feature pulls correctly from fixture claims |
| 129.2 | Training set builder | `scripts/build_steward_training_set.py` → `tests/fixtures/steward_training.jsonl` | Excludes migration-origin archives; ~2,900 pos + 3× neg |
| 129.3 | Training script | `scripts/train_steward_classifier.py` — chronological 80/20, LogisticRegression + isotonic CV | Outputs `artifacts/steward-classifier-v1.joblib` + eval report |
| 129.4 | Steward integration | Patch `steward.py::_decide_promotion` to call classifier with fallback to current additive formula when artifact missing | `rm artifact && run_cycle` doesn't crash |
| 129.5 | Regression test | `tests/test_steward_classifier.py` — asserts recall ≥0.70 @ FPR ≤0.05 on held-out split | CI gate green |
| 129.6 | Ablation writeup | `artifacts/steward-classifier-eval-2026-04-23.md` — per-feature ablation + calibration curve | Document signed off |

---

## #75 — Run graphify on 15+ legacy projects

**Status:** blocked on user input. /graphify is an interactive Claude skill, not a batch. I need a concrete list of which ≥15 projects under `G:\_OneDrive\OneDrive\Desktop\Py Apps\` to run it against, AND whether to run them sequentially in this session or hand off to a dedicated graphify session (recommended — graphify sessions produce a lot of intermediate artifacts and are best kept isolated).

Suggested criteria if you want me to pick:

1. Active projects without `graphify-out/GRAPH_REPORT.md` in repo root
2. Projects ≥500 files but ≤10k files (graphify quality peaks in this range)
3. Exclude anything under `archive/` or clearly-deprecated folders

If you say "go, pick any 15", I will enumerate and schedule.

---

## Parallelization plan (this session)

- **Agent A** — implement #127.1 → #127.5 in isolated worktree. Commits on branch `omni/feat-entity-extraction-2026-04-23`. Layer 2 deferred.
- **Agent B** — implement #129.1 → #129.5 in isolated worktree. Commits on branch `omni/feat-steward-classifier-2026-04-23`. Ablation writeup deferred.
- Coordinator role: qa-verifier. I (main session) will NOT spawn a third and instead monitor, merge each agent's branch when it reports metric-acceptance, and cross-cut any integration issues.

Cap at 2 concurrent agents (per claim 11761; 4-way fanout caused 3/4 rate-limits in the prior session).

---

## Definition of done (both arcs)

1. Acceptance metric hit, recorded in the respective artifacts file.
2. Full test suite (`python -m pytest tests/ -q`) still passes.
3. Branch merged to main through the commit-guard-safe path (branch → push → ff-merge main).
4. A `claim_type=architecture` ingest summarizing what shipped + the measured before/after.
