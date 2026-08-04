# MemoryMaster autoresearch convergence audit delta — 2026-08-04
# Covers: SQLite-only retrieval, graph, capture, package, and supply-chain convergence evidence.
# Key terms: LongMemEval, graph support, capture replay, OAuth QA, CycloneDX, Gitleaks, Docker Scout.
# Read when: deciding what the autoresearch program proved and what still gates a PR or release.
# Baseline: vNext branch before the 2026-08-03 autoresearch program; artifact evidence at 954a6e7.
# Boundary: temporary databases and local artifacts only; PostgreSQL was explicitly waived.
# Verdict: local convergence, authenticated image scan, and full QA passed; public and longitudinal gates remain.

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
| Comparable full QA | 46.2% (231/500) | 46.4% (232/500) | +0.2 points; allowed floor was -2.0 points |

The graph fix reserves a bounded slot for authoritative claim-store results
rehydrated from active, authorized edge supports. It does not allow the graph to
answer independently or bypass lifecycle, tenant, scope, sensitivity, or
principal checks. The LongMemEval result file now persists its gate metrics at
the documented top level, preventing a successful evaluation from being
misreported as a regression by shell automation.

## Verification evidence

| Gate | Result |
|---|---|
| Test collection | 4,424 tests collected after five focused Docker/QA hardening tests |
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
| Local image | `sha256:a5cd9cc9ea7c2669f99bb42ee2865d7ae3c8fe0097c3421b7d8f86cb3d7a4d8a`; built locally and not pushed |
| Docker Scout | Zero critical, high, medium, or low vulnerabilities; authenticated immutable-image gate passed |

The fail-closed supply-chain runner passed `gitleaks_history`,
`pip_audit_project`, `pip_audit_release_extras`, `validate_sbom`, and
`docker_scout_1`. The prior Debian-slim image reported two critical and two
high Perl base-layer vulnerabilities. Rebuilding both stages from the pinned
official Python 3.12 Alpine image removed those findings while preserving the
wheel build and runtime smoke. Docker authentication was scoped to the scanner
subprocess and no credential was copied into the repository or image.

The wheel build emitted a setuptools warning that the legacy license table and
classifier should migrate before 2027-02-18. It is a packaging-maintenance
follow-up, not a failure of the current artifact.

## Remaining gates

1. Expand the private capture/graph evaluation corpus enough to support the
   90/90/85 precision thresholds statistically.
2. Continue the already-established seven-day observation described by the
   governed-capture audit.
3. Obtain explicit public-release authorization before pushing this branch,
   opening a PR on the public remote, tagging, or publishing a package.

The earlier replacement 24-hour observation and candidate-write activation
already passed. This autoresearch program did not touch the live database,
replace scheduled tasks, push commits, create a PR, or publish anything.

The operator explicitly authorized the full QA budget. Two comparable
500-question passes used keyless OpenCode 1.18.13 with
`openai/gpt-5.4-mini` at medium effort and no API keys. Baseline accuracy was
46.2 percent (231/500, 2,808,927 tokens, 7,358.747 seconds); the retained result
was 46.4 percent (232/500, 2,720,061 tokens, 7,155.010 seconds). The +0.2-point
delta passes the no-more-than-two-point regression gate. The improved pass was
aggregated from 50 exact ten-question chunks with zero failed attempts; the
aggregator validated question identity/order, judge configuration, status, and
artifact hashes before emitting the complete result.

The baseline QA artifact SHA-256 is
`c2ee71ec184b51239fe3591e0f8bcce63d39c3eac1c7bfae83aea29dfca9b742`;
the retained QA artifact SHA-256 is
`b3861d4f6cc816e4bd1df59d904e045eeb3589fd7f75969d03c092dd55ee3d36`.

## Rollback and evidence handling

All retained code changes are atomic commits on the isolated autoresearch
branch. No active-database rollback condition fired. Disposable environments,
wheel/SBOM reports, and scanner output remain ignored local evidence; raw
secret-scan findings and private database details are not committed.
