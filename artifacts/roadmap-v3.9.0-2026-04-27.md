# Roadmap v3.9.0 — "Steal everything good"

**Branch**: `omni/v3.9.0-steal-and-test`
**Trigger**: Survey of 6 active memory/code-graph projects identified 9 portable features, ordered S→B by impact. Ship them all in one big release with full unit + E2E coverage. Goal: lift our N=953 precision@5=0.105 baseline by attacking GRAPH-flat with multiple approaches simultaneously.

## Source for each feature

See `artifacts/steal-from-others-2026-04-27.md` for the full survey + per-feature analysis.

## Features (9, ordered TIER S → B)

| # | Feature | Source | Acceptance criterion | Test type |
|---|---|---|---|---|
| F1 | claim_type-aware ranking | MemPalace "Halls" | Recall ranking factors `claim_type` of query (from classify hook) against claim_type of candidates. Measurable: precision@5 lift on prompts with strong type signal. | unit + recall eval |
| F2 | MemPalace entity regex port | MemPalace v3.3.3 | CamelCase preserved (MemPalace not Mem+Palace), tightened hyphenated (no `multi-word` false positive), git-author entity source. | unit on entity_extractor |
| F3 | cwd-from-transcript scope | MemPalace v3.3.3 | Auto-ingest hook reads `cwd` from JSONL session file, prefers it over slug-decode for scope derivation. | unit on auto_extractor |
| F4 | wiki-validate auto-fix CLI | gbrain v0.22.4 | New CLI `memorymaster wiki-validate <path> [--fix] [--audit]`. 4 fixable codes: missing `description`, missing `date`, missing `tags`, missing `type`. | unit on new module + integration |
| F5 | Two-pass retrieval (entity-fanout pass) | gbrain v0.21.0 "Cathedral II" | After lexical recall returns top-N, a 2nd pass fans out via entity_aliases co-mention. Env-gated `MEMORYMASTER_RECALL_TWO_PASS=1`. | recall eval N=953 |
| F6 | Closets — search-side wiki-pointer boost | MemPalace v3.3.0 | New table `closets` (regex-derived pointers per wiki article). Search hits closets first as boost, claims direct still as floor. | unit + recall eval |
| F7 | federated-graphify MCP tool | graphify v0.5.0 | New MCP tool `federated_query_graphify` merges graphify-out across N indexed projects + filter by `repo` tag. | unit on MCP wrapper |
| F8 | Structural call-edges between claims (parent-scope chunking) | gbrain v0.21.0 | New table `claim_edges` (claim_a calls/references claim_b, with hop weight). Walked in 2nd pass of F5. | unit + recall eval |
| F9 | Cynical-deletion audit sweep | claude-mem v12.4.7 | Identify ≥10 silent `try: except: pass` defenders/tolerators in our code; replace with strict errors at boundaries OR documented swallows. | code review report |

## Plan (5 waves)

### Wave 1 — Quick wins (parallel, each 1-4h)
- F1 claim_type ranking
- F2 entity regex port
- F3 cwd-from-transcript
- F4 wiki-validate CLI

### Wave 2 — Recall-side experiments (sequential, depend on shared infra)
- F5 two-pass retrieval (env-gated)
- F6 closets table + search boost (env-gated)
- Re-run B1 grid + N=953 eval after each, snapshot deltas

### Wave 3 — MCP / cross-project
- F7 federated-graphify MCP tool

### Wave 4 — Structural edges (the big bet on GRAPH-flat fix)
- F8 claim_edges schema + extractor + 2nd-pass walker
- Re-run B1 grid + N=953 eval. Compare against F5/F6 deltas.

### Wave 5 — Audit + tests + ship
- F9 cynical-deletion audit
- Full pytest run, ensure no regressions
- E2E test (full ingest → recall → wiki-absorb → wiki-validate)
- v3.9.0 commit + ff-merge + push + tag + PyPI + GitHub Release

## Acceptance criteria for v3.9.0 ship

1. All 9 features implemented or honestly explained as deferred.
2. New test files: `test_claim_type_ranking.py`, `test_entity_regex_v3.py`, `test_cwd_scope.py`, `test_wiki_validate_cli.py`, `test_two_pass_recall.py`, `test_closets.py`, `test_federated_graphify_mcp.py`, `test_claim_edges.py`. Each with ≥5 cases.
3. E2E test that exercises the full pipeline.
4. N=953 precision@5 eval re-run, with delta documented (positive or honest null).
5. `pytest -q` shows green (no test_*.py file with fails outside known-skip).
6. CHANGELOG entry per feature, including null-result documentation if F5/F6/F8 don't lift the metric.

## Honest non-goals

- I will NOT chase a precision@5 lift if the experiment honestly shows null. I will document and ship.
- F8 (structural call-edges) may turn into a research spike if the schema design takes longer than expected. Acceptable to ship as "F8 partial: schema only" with the walker deferred to v3.10.

## Estimate

12-20h end-to-end. Single-track execution where Waves 2-4 require sequential validation. Wave 1 is parallelizable.
