# Campaign manifests: the first three

## Common to all three

- **Identity.**
  - Base SHA: the commit that lands this audit on `main`.
  - Hardware: i9-14900K, 64 GB RAM, Windows 10, Python 3.12.4.
  - Live features: profile 1, graph observations 1, Jev live. The benches remove all of it.
  - Providers: none. Each campaign makes **0 provider calls**.
- **Isolation.**
  - One campaign at a time, each in its own worktree and branch (`autoresearch/<campaign>`).
  - The controller runs with `--execution-policy workspace-write`, never `danger-full-access`.
  - Nothing is merged to `main` without human review.
- **Protected, never in `--scope`:**
  - `benchmarks/`, `tests/` and `scripts/`
  - the baseline files in this directory
  - `pyproject.toml`, `noxfile.py`, migrations and schema
  - Changing a fingerprint, a threshold or a fixture to pass is grounds for rejection.
- **Comparison method.**
  - Run baseline and candidate in the same session, in alternating order, as independent processes.
  - 7 or more repeats each.
  - A result inside the spread (max − min of the baseline) is inconclusive, not an improvement.
- **Stop conditions.** Stop on `--max-iterations`, on 3 consecutive inconclusive or rejected trials, or on any Guard failure at the baseline.
- **Rollback.** The controller reverts each discarded trial with `git revert`. The campaign branch is disposable. These campaigns touch no data: every Verify and Guard works on temp DBs.

---

## Campaign 1: A, cold tokenizer statistics: start now (highest live impact)

| Field | Value |
|---|---|
| Hypothesis | `_corpus_stats` (`recall_tokenizer.py:182-224`) scans and tokenizes the whole live corpus on every cold call: about 16-17 ms per 1,000 claims, **752 ms at 45k**. A cheaper exact computation inside the module gives the same tokens. Falsified if no in-scope change keeps the fingerprint and lowers the cold time beyond the spread. |
| Editable scope | `memorymaster/recall/recall_tokenizer.py` |
| Out of scope | Changing the triggers or the meaning of `corpus_generation` would reduce invalidations, but it needs a migration and a human decision. Writing persistent state to disk or the DB from the read path is forbidden. |
| Verify | `python -P benchmarks/tokenizer_cold_bench.py --sizes 20000 --repeats 7 --corpus-cache <cache>`; metric `metric` = cold median ms at 20k; direction `lower`. |
| Baseline | 323.4 ms (max 366.1) at 20k; warm 6.2 ms (`baseline-tokenizer-cold.json`). |
| Target | ≤ 100 ms cold at 20k. Justification: the 1k cold is 26.8 ms, which is the floor of process startup, connection and generation read. 100 ms means the scan stops dominating. |
| Guard | `python -P benchmarks/tokenizer_cold_bench.py --sizes 1000 5000 20000 45000 --repeats 1 --corpus-cache <cache> --guard .planning/audits/2026-10-04-autoresearch/baseline-tokenizer-cold.json && python -m pytest tests/test_recall_tokenizer.py tests/test_autoresearch_bench_guards.py -q -p no:cacheprovider` |
| Hard constraints | The same tokens for the same query and corpus. Generation-based invalidation still applies. Archived and superseded claims stay excluded. The read path stays read-only. |
| Data | Synthetic and seeded corpora. `<cache>` is a directory outside the repo; build it once (about 30 min for the 4 sizes). |
| Budget | `--max-iterations 12`, `--timeout-seconds 900`. |

**Approvals still needed:** operator approval to install `codex-autoresearch` (see `EXTERNAL-CONTROLLERS.md`), and to create the worktree/branch.

**Launch** (verify the exact flag spelling against the installed skill's `scripts/autoresearch.py`; `<cache>` lives outside the repo):

```
git worktree add ../mm-ar-tokenizer -b autoresearch/tokenizer-cold
cd ../mm-ar-tokenizer
python -P benchmarks/tokenizer_cold_bench.py --sizes 1000 5000 20000 45000 --repeats 1 --corpus-cache <cache>   # build the cache once (~30 min)
python <skill>/scripts/autoresearch.py init --repo .   --verify "python -P benchmarks/tokenizer_cold_bench.py --sizes 20000 --repeats 7 --corpus-cache <cache>" --metric-key metric   --direction lower --target 100   --guard "python -P benchmarks/tokenizer_cold_bench.py --sizes 1000 5000 20000 45000 --repeats 1 --corpus-cache <cache> --guard .planning/audits/2026-10-04-autoresearch/baseline-tokenizer-cold.json && python -m pytest tests/test_recall_tokenizer.py -q -p no:cacheprovider"   --scope memorymaster/recall/recall_tokenizer.py --max-iterations 12 --timeout-seconds 900
python <skill>/scripts/autoresearch.py launch --repo . --execution-policy workspace-write
```

**Codex prompt:**

> Optimise only `memorymaster/recall/recall_tokenizer.py` so a cold `extract_query_tokens` costs less on large corpora while returning exactly the same tokens. The evaluator is `benchmarks/tokenizer_cold_bench.py`. Never edit benchmarks, tests, baselines, migrations or triggers, and add no on-disk state. Do not change which claims count (archived and superseded stay out) or generation invalidation. Stop after 12 iterations or 3 inconclusive trials in a row.

---

## Campaign 2: B, context packing (`pack_context`): start now

| Field | Value |
|---|---|
| Hypothesis | `pack_context` re-serializes every included row for each candidate (`context_optimizer.py:360-411`). Incremental accounting that is exact against the final render gives the **same output** for less work. Falsified if no change preserves every fingerprint while reducing time beyond the spread. |
| Editable scope | `memorymaster/recall/context_optimizer.py` |
| Verify | `python -P benchmarks/pack_context_bench.py --repeats 9`. The last line is JSON; metric key `metric` = p95 ms for 200 rows in JSON; direction `lower`. |
| Baseline | p95 501.4 ms, median 457.8 ms (`baseline-pack-context.json`). |
| Target | ≤ 100 ms p95. Justification: the same 200 rows take 5.5 ms in XML, so the remainder is JSON re-serialization, not inherent work. Minimum relevant effect: −20 % and outside the spread. |
| Guard | `python -P benchmarks/pack_context_bench.py --repeats 1 --guard .planning/audits/2026-10-04-autoresearch/baseline-pack-context.json && python -m pytest tests/test_context_optimizer.py tests/test_context_optimizer_provider.py tests/test_autoresearch_bench_guards.py -q -p no:cacheprovider` |
| Hard constraints | Identical output, `claims_included`, `tokens_used` and order in all 3 formats. Budget respected. JSON escaping and Unicode correct. Same error when the budget is below the minimum. |
| Data | Synthetic and seeded (`_rows`, seed 20261004). |
| Budget | `--max-iterations 15`, `--timeout-seconds 600`. CPU only. |

**Approvals still needed:** the same as campaign 1. Run it after campaign 1, never in parallel on the same machine (the measurements compete).

**Launch** (verify the exact flag spelling against the installed skill's `scripts/autoresearch.py`):

```
git worktree add ../mm-ar-pack -b autoresearch/pack-context
cd ../mm-ar-pack
python <skill>/scripts/autoresearch.py init --repo . \
  --verify "python -P benchmarks/pack_context_bench.py --repeats 9" --metric-key metric \
  --direction lower --target 100 \
  --guard "python -P benchmarks/pack_context_bench.py --repeats 1 --guard .planning/audits/2026-10-04-autoresearch/baseline-pack-context.json && python -m pytest tests/test_context_optimizer.py tests/test_context_optimizer_provider.py -q -p no:cacheprovider" \
  --scope memorymaster/recall/context_optimizer.py --max-iterations 15 --timeout-seconds 600
python <skill>/scripts/autoresearch.py launch --repo . --execution-policy workspace-write
```

**Codex prompt:**

> Optimise only `memorymaster/recall/context_optimizer.py` so `pack_context` produces byte-identical output in less time. The evaluator is `benchmarks/pack_context_bench.py`; never edit benchmarks, tests, baselines or thresholds. Every trial must keep every fingerprint. The JSON path re-renders the whole prefix for each candidate block (`_measured_context`); look for exact incremental accounting. Respect `token_budget`, escaping and Unicode. Stop after 15 iterations or 3 inconclusive trials in a row.

---

## Campaign 3: D, warm recall in the shared server: measure first, then launch

| Field | Value |
|---|---|
| Hypothesis | The live warm recall median (660 ms in `hook_recall`) is dominated by one stage of the pipeline, not by the warm tokenizer (6 ms). Making that stage cheaper keeps the ranking identical. |
| Editable scope (provisional) | `memorymaster/recall/context_hook.py`, `memorymaster/recall/retrieval.py`, `memorymaster/recall/candidate_pool.py`. It will be narrowed to the file holding the dominant stage once it is profiled. |
| Verify | `python -P benchmarks/recall_warm_bench.py --size 45000 --confirmed-share 0.1 --repeats 7 --corpus-cache <cache>`; metric `metric` = warm p95 ms; direction `lower`. |
| Baseline | warm p95 83.1 ms (median 69.3); after one write median 681 ms; cold 696 ms (`baseline-recall-warm.json`). |
| Target | Set after profiling. If campaign A removes the rescan, D's metric becomes warm p95 (83 ms); if not, the after-write median. The target is not set before profiling. |
| Guard | `python -P benchmarks/recall_warm_bench.py --size 45000 --confirmed-share 0.1 --repeats 1 --corpus-cache <cache> --guard <baseline> && python -m pytest tests/test_recall_delivery_contract.py tests/test_retrieval_profile.py tests/test_ro_recall.py tests/test_recall_tokenizer.py -q -p no:cacheprovider` |
| Hard constraints | Identical ranked ids for every query. Scope, tenant, authorization and sensitivity filters unchanged. Read-only. 0 calls to Qdrant or providers. |
| Budget | `--max-iterations 10`, `--timeout-seconds 1200`. |

**Before launch:** profile `recall()` on the bench corpus and fix the scope and target.

**Approvals still needed:** the same as campaign 1. Run it after campaign 1, whose result changes this campaign's metric.
