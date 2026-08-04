# MemoryMaster autoresearch convergence audit delta — 2026-08-04
# Covers: SQLite-only retrieval, graph, capture, package, and supply-chain convergence evidence.
# Key terms: LongMemEval, graph support, capture replay, CycloneDX, Gitleaks, Docker Scout.
# Read when: deciding what the autoresearch program proved and what still gates a PR or release.
# Baseline: vNext branch before the 2026-08-03 autoresearch program; artifact evidence at 954a6e7.
# Boundary: temporary databases and local artifacts only; PostgreSQL was explicitly waived.
# Verdict: local convergence passed; authenticated image scan, full QA, and public actions remain gated.

## Outcome

The six bounded autoresearch phases are complete. They improved benchmark
iteration time, production-path query latency, retrieval rank quality, and
graph-supported recall while preserving confirmed-only trusted retrieval,
scope authorization, evidence citations, replay safety, and zero provider calls
in deterministic benchmarks. Capture already exceeded its acknowledgement SLO,
so no unnecessary product change was made for that phase.

This is a delta over the governed-capture audit, not a new whole-project audit.
It does not supersede the product roadmap or the earlier live activation and
24-hour observation evidence.

## Measured improvements

| Surface | Baseline | Retained result | Guard |
|---|---:|---:|---|
| LongMemEval-S 25-question harness | 105.168 s | 66.569 s | Exact rankings and metrics; zero provider calls |
| Temporary-SQLite query p95 | 52.265 ms | 38.608 ms | Zero misses; governed result counts preserved |
| LongMemEval-S full MRR | 0.9021 | 0.9076 | R@5 0.966 -> 0.972; R@10 held at 0.984; zero provider calls |
| Graph-focused top-five hits | 0/6 | 6/6 | Zero forbidden hits; active authorized supports only |
| Capture acknowledgement p95 | n/a | 107.0 ms worst of five runs | Below 500 ms SLO; zero replay/integrity failures |

The graph fix reserves a bounded slot for authoritative claim-store results
rehydrated from active, authorized edge supports. It does not allow the graph to
answer independently or bypass lifecycle, tenant, scope, sensitivity, or
principal checks. The LongMemEval result file now persists its gate metrics at
the documented top level, preventing a successful evaluation from being
misreported as a regression by shell automation.

## Verification evidence

| Gate | Result |
|---|---|
| Test collection | 4,419 tests collected |
| Full non-ML | 4,250 passed, 71 skipped, 97 deselected, 1 xfailed, 2 warnings in 792.78 s |
| ML/retrieval | 97 passed, 4,322 deselected, 1 warning in 379.74 s |
| Graph focused | 49 passed; benchmark 6/6 with zero forbidden hits and provider calls |
| Capture adversarial | 62 passed, 1 optional-parser skip; five repeated p95 runs all below 500 ms |
| SQLite/demo | 61 migration, restore, lineage, lifecycle, and disposable-demo tests passed |
| Release contracts | 86 passed; generated release truth and Ruff passed |
| Static hygiene | `git diff --check` passed |

The first full-suite attempt used the worktree source with stale global
`memorymaster` distribution metadata at version 3.2.0. That environment failed
only the version truth assertion. Re-running in an isolated environment with
the checkout installed as version 4.5.0 produced the full pass above; the global
live package was not replaced. Clean-wheel smoke tests also ran from a newly
created empty directory because an unrelated `inspect.py` in the system temp
directory can shadow Python's standard-library module on Windows.

## Artifact and supply-chain evidence

| Artifact/check | Evidence |
|---|---|
| Wheel | `memorymaster-4.5.0-py3-none-any.whl`, 922,440 bytes, 342 entries |
| Wheel SHA-256 | `fb5b65b9e25b96b274029b2224066b05c546725848c0ac620334c3e70e55b80a` |
| Clean install | Default profile excludes PDF/DOCX parsers; `capture` profile includes `pypdf` and `python-docx`; both import and CLI smokes passed |
| Package contents | No tests, benchmarks, autoresearch artifacts, environment files, or Git metadata; required capture, ontology, and migration modules present |
| SBOM | CycloneDX 1.6, 7 components, root bound to `pkg:pypi/memorymaster@4.5.0` and the exact wheel hash |
| SBOM SHA-256 | `fdea9bc333afa591fb8e85b563b95c5883bc229fe13a542e4b30f09485b32f0a` |
| Dependencies | Strict OSV project and release-extra audits passed with zero known vulnerabilities after updating only disposable-environment bootstrap `pip` |
| Secret history | Reviewed fail-closed full-history Gitleaks check passed |
| Local image | `sha256:5c179279dc1a59fb5d997dbb4e5a4e517b5f13f45cfdf70c69676f6487f35ea8`; built locally and not pushed |

The fail-closed supply-chain runner passed `gitleaks_history`,
`pip_audit_project`, `pip_audit_release_extras`, and `validate_sbom`. Its fifth
check, `docker_scout_1`, stopped with a nonzero result because Docker Scout
requires a logged-in Docker account. The result was not waived or translated
into a pass. No credential was requested or stored.

The wheel build emitted a setuptools warning that the legacy license table and
classifier should migrate before 2027-02-18. It is a packaging-maintenance
follow-up, not a failure of the current artifact.

## Remaining gates

1. Authenticate Docker Scout and rerun the immutable local-image gate.
2. Run comparable before/after full QA with the same OAuth-backed judge.
3. Expand the private capture/graph evaluation corpus enough to support the
   90/90/85 precision thresholds statistically.
4. Continue the already-established seven-day observation described by the
   governed-capture audit.
5. Obtain explicit public-release authorization before pushing this branch,
   opening a PR on the public remote, tagging, or publishing a package.

The earlier replacement 24-hour observation and candidate-write activation
already passed. This autoresearch program did not touch the live database,
replace scheduled tasks, push commits, create a PR, or publish anything.

## Rollback and evidence handling

All retained code changes are atomic commits on the isolated autoresearch
branch. No active-database rollback condition fired. Disposable environments,
wheel/SBOM reports, and scanner output remain ignored local evidence; raw
secret-scan findings and private database details are not committed.
