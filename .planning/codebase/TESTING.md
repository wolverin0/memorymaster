# Testing Patterns

**Analysis Date:** 2026-08-17
**Version analysed:** MemoryMaster 4.7.6, commit `c832df4`

## Test Framework

**Runner:** pytest `>=8.2` (`pyproject.toml:48`, extra `dev`), config `pytest.ini` at the repo root.
**Plugins:** `pytest-cov>=6.0`, `pytest-timeout>=2.3`.
**Assertions:** plain `assert` — no assertion library.
**Mocking:** `monkeypatch` (187 files) is the default; `unittest.mock` appears in only 52 files.

**Global config (`pytest.ini`):**
- `testpaths = tests`
- `addopts = -p no:cacheprovider`
- `norecursedirs = artifacts .pytest_cache .tmp_pytest`
- `timeout = 600`, `timeout_method = thread` (signal-based timeouts are unavailable on Windows)

**Root `conftest.py`** inserts the repo root into `sys.path` so tests can `from scripts import ...` — needed because `pyproject.toml` scopes `packages.find` to `memorymaster*`, which drops the root from `sys.path` under `pip install -e .`.

## Run Commands

```bash
# Full suite — THE canonical command
pytest tests/ -m "not ml"

# ML/vector tests, in isolation only
pytest tests/ -m ml

# One file / one test
pytest tests/test_backend_parity.py -q
pytest tests/test_service_coverage.py::test_ingest_and_query -q

# Coverage
pytest tests/ -m "not ml" -q --cov=memorymaster --cov-report=term-missing
```

**`-m "not ml"` is mandatory.** The `ml` files load torch / sentence-transformers / Qdrant paths that randomly SIGSEGV (exit 139, "Windows fatal exception: access violation") or hang inside real-model loads when mixed into a full run on Windows (`pytest.ini:2-7`). CI uses the same flag (`.github/workflows/ci.yml`, `pytest tests/ -m "not ml" -q --tb=short`).

**Suite size, measured 2026-08-17:** `4591 selected / 4688 collected (97 deselected)` across 399 `test_*.py` files. A full run takes roughly 20 minutes.

**Run it in alphabetical chunks, in the foreground.** Long background runs get killed before finishing, so split it:

```bash
pytest "tests/test_[a-c]*.py" -m "not ml"   # 74 files
pytest "tests/test_[d-i]*.py" -m "not ml"   # 89 files
pytest "tests/test_[j-q]*.py" -m "not ml"   # 99 files
pytest "tests/test_[r-z]*.py" -m "not ml"   # 137 files
```

> `CONTRIBUTING.md` still says "932 tests across 66 test modules" and `pytest tests/ -q` without a marker filter. That is stale; `pytest.ini` is authoritative.

## Markers

| Marker | Meaning | How it runs |
|---|---|---|
| `ml` | embeddings/vector tests touching torch, sentence-transformers, Qdrant | **Excluded from the default run.** `pytest -m ml` in isolation. 9 files: `test_embeddings_coverage.py`, `test_embeddings_degraded_signal.py`, `test_qdrant_backend.py`, `test_qdrant_reconcile.py`, `test_recall_vector_fallback.py`, `test_service_embedding_toctou.py`, `test_v313_e2e.py`, `test_vector_search.py`, `test_verbatim_store_qdrant.py` |
| `postgres` | needs a reachable Postgres DSN | Skips cleanly when unset; applied via `pytest.param("postgres", marks=…)` in `conftest.py:161` and in `test_postgres_*.py` |
| `unit` | fast, offline (no I/O, subprocess, or DB) | 7 files |
| `soak` | long chaos harness `tests/soak/chaos_soak.py` | Never in the default suite; run explicitly |
| `calibration` | dev-box threshold calibration | Skipped unless `MEMORYMASTER_CALIBRATE=1`; `test_resolve_project_calibration.py` |

## Test File Organization

**Location:** flat under `tests/`, separate from source (not co-located). Three subdirectories:
- `tests/fixtures/` — JSONL/JSON eval corpora: `sensitivity_adversarial.jsonl`, `sensitivity_adversarial_v2.jsonl`, `steward_eval.jsonl`, `steward_training.jsonl`, `classify_eval.jsonl`, `entity_extraction_eval.jsonl`, `paper_research_eval_v1.jsonl`, `qrels_search.json`, `atlas/`
- `tests/integration/` — `test_extract_llm_ollama_live.py` (needs a live Ollama)
- `tests/soak/` — `chaos_soak.py`, `soak_slice.py`, `soak_writers.py`

**Naming:** `test_<subject>.py`. Extensions to an existing subject use `_extra` / `_v2` / `_coverage` suffixes (`test_dream_bridge_extra.py`, `test_db_merge_coverage_v2.py`). Release-gate suites are versioned (`test_v313_e2e.py`, `test_v390_e2e.py`, `test_v47_operational_acceptance.py`). Test function names are long and state the asserted behaviour: `test_gradual_size_budgets_prevent_facade_regrowth`.

**Structure:** module-level functions, not classes. Module docstring states what the file gates and why (`tests/test_backend_parity.py:1-11`). Section separators are `# ---- comment ----` banners. Local `_helper()` builders sit at module top.

## Autouse Hermeticity Fixtures (`tests/conftest.py`)

Four autouse fixtures run for every test. Know them before debugging a "works alone, fails in suite" case:

| Fixture | File:line | What it does |
|---|---|---|
| `_explicit_local_mcp_auth` | `conftest.py:117` | Sets `MEMORYMASTER_MCP_AUTH_MODE=local-trusted` so legacy MCP calls exercise the named local profile |
| `_hermetic_snapshot_dir` | `conftest.py:196` | Redirects `MEMORYMASTER_SNAPSHOT_DIR` to a tmp dir — otherwise `run_cycle`'s integrity phase writes `mm-YYYYMMDD.db` snapshots into the operator's real `~/.memorymaster/snapshots/` and the keep-3 rotation could evict **real** production snapshots |
| `_hermetic_wal_discipline` | `conftest.py:212` | Deletes `MEMORYMASTER_WAL_DISCIPLINE` and `MEMORYMASTER_INITDB_FASTPATH` (both are `setx`-set machine-wide during dogfood weeks) and redirects `MEMORYMASTER_SPOOL_DIR` to tmp. Tests that need those flags opt back in with `monkeypatch.setenv` |
| `_cleanup_case_artifacts` | `conftest.py:237` | Creates `.tmp_cases/` and prunes it **after** the test only — pruning before caused DB corruption when `mkstemp` reused a path |

**Rule:** a test that needs a machine-wide flag must set it itself via `monkeypatch.setenv`. Never rely on ambient env.

## Backend Parity Fixture

`parametrize_backends` (`tests/conftest.py:158-174`) yields `(backend_name, MemoryService)` for SQLite and Postgres so one test body asserts identical observable behaviour on both:

```python
def test_parity_ingest_then_list(parametrize_backends):
    backend, svc = parametrize_backends
    _ingest(svc, "parity claim alpha")
    claims = svc.store.list_claims(status="candidate")
    assert sorted(c.text for c in claims) == [...], f"{backend}: unexpected claim set"
```

SQLite always runs (file-based, tmp_path). Postgres runs only when **all** of these hold, else it skips with `BLOCKED-EXTERNAL:` (`conftest.py:31-46`):
- `MEMORYMASTER_TEST_POSTGRES_DSN` (admin) and `MEMORYMASTER_TEST_POSTGRES_APP_DSN` (app) are both set and **different**
- `MEMORYMASTER_TEST_POSTGRES_RLS_DISPOSABLE=1`
- neither DSN matches a live `DATABASE_URL` / `POSTGRES_DSN` / `MEMORYMASTER_POSTGRES_DSN` — the harness **fails** rather than risk touching a real database

Role attributes are validated before use (`_validate_disposable_postgres_roles:64`): admin must be SUPERUSER or BYPASSRLS, the app role must have no elevated attribute, and both must target the same disposable database.

Local Postgres for this: `docker-compose.postgres.yml` (host port 6543).

## Common Patterns

**Temp state:** `tmp_path` in 270 files — build a fresh DB per test, never reuse a shared fixture DB.

```python
db = tmp_path / "parity-sqlite.db"
svc = MemoryService(db, workspace_root=tmp_path)
svc.init_db()
```

**Env manipulation:** always `monkeypatch.setenv` / `monkeypatch.delenv(..., raising=False)`, never `os.environ[...] = ...`.

**Skip vs fail:** missing external dependency → `pytest.skip("BLOCKED-EXTERNAL: ...")`; a misconfigured or unsafe harness → `pytest.fail(...)`. Do not silently pass.

**Subprocess isolation** for import-order and clean-process behaviour:

```python
result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=False)
assert result.returncode == 0
```

Used to prove the install probe survives a process without `importlib.util` (`test_release_truth.py:48`) and that the CLI imports with `httpx` blocked by a `MetaPathFinder` (`test_release_truth.py:104`).

**Source-text assertions** — several guard tests read source files and assert on their content rather than behaviour (size budgets, required imports, workflow contents). AST parsing via `ast.parse` for structural facts (`test_architecture_budgets.py:16-19`).

## Notable Guard Tests

**`tests/test_architecture_budgets.py`** — non-growth budgets and facade contracts:
- line caps: `core/service.py` ≤ 2450, `surfaces/dashboard.py` ≤ 1550, `DashboardRequestHandler` ≤ 720, and ≤ 800 for `core/services/integration.py`, `surfaces/dashboard_read_models.py`, `surfaces/dashboard_commands.py`
- every function in those three files ≤ 50 lines
- `MemoryService` must remain a subclass of `IntegrationService`, with `upsert_source_item` / `create_action_proposal` living only in the parent
- `dashboard.py` must import from both the read-model and command modules
- `docs/compatibility.md` must keep the dated removal gate `2026-09-30` for `memorymaster.service`

**`tests/test_release_truth.py`** — single-source release facts:
- `memorymaster.__version__` and the dashboard HTML banner both equal `pyproject.toml`'s version
- `docs/generated/release-truth.md` is committed and current (CI runs `scripts/generate_release_truth.py --check`)
- the test-inventory counter is platform-independent (CRLF/LF, sync + async + methods, only `test_*.py`)
- root `ROADMAP.md` is the only authoritative roadmap; `ROADMAP-v3.2.md`, `roadmapres.md`, `docs/ROADMAP.md` must each be ≤ 12 lines and point back to it
- `.github/workflows/publish.yml` must publish only the verified downloaded artifact (`verify-artifact` job, `verified-dist` used twice, `qrels`+`release_truth` gates present, eval **not** gating)
- a minimal CLI import must work with `httpx` unavailable

**Other high-value clusters:** `test_sensitivity_filter_*` + `test_filter_bypass_hardening.py` + `test_mcp_filter_bypass.py` (the ingest firewall) · `test_backend_parity.py` / `test_storage_parity.py` / `test_postgres_parity.py` · `test_postgres_rls_*` / `test_tenant_isolation.py` · `test_supply_chain_contracts.py` · `test_deployment_contracts.py` · `test_qdrant_retrieval_quarantine.py` (asserts the quarantine holds).

## Coverage & Performance

No enforced coverage threshold — `pytest-cov` is available but CI has no `--cov-fail-under`.

Performance and quality are gated by separate CI jobs, not by the unit suite:
- `benchmarks/perf_smoke.py` against `benchmarks/slo_targets.json`, best-of-3 (shared runners swing ~5×)
- `scripts/eval_memorymaster.py` (`continue-on-error: true` — informational)
- deployment smoke: stdio MCP handshake, docker build + `init-db`, dashboard `/healthz` + `/readyz`, authenticated streamable-HTTP MCP handshake, `helm lint` + `helm template` for both profiles

## Test Types

- **Unit** — the bulk; per-module behaviour with `tmp_path` SQLite and monkeypatched env.
- **Contract / guard** — architecture budgets, release truth, deployment contracts, policy fingerprints, supply-chain contracts.
- **Parity** — same body, both backends.
- **Evaluation** — corpus-driven quality gates reading `tests/fixtures/*.jsonl` (`test_classify_hook_f1.py`, `test_qrels_regression.py`, `test_recall_precision_at_5.py`, `test_confusion_matrix_eval.py`).
- **Latency** — `test_recall_latency.py`, `test_dashboard_latency.py`, `test_classify_hook_latency.py`.
- **Integration (live)** — `tests/integration/test_extract_llm_ollama_live.py`, opt-in.
- **Soak/chaos** — `tests/soak/chaos_soak.py`, `soak` marker, run explicitly.
- **E2E** — no browser layer; `test_v313_e2e.py` / `test_v390_e2e.py` are in-process end-to-end flows.

---

*Testing analysis: 2026-08-17*
