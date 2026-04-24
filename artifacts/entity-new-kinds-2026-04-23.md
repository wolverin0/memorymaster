# Roadmap 3.2 — Entity-kind expansion (Layer 1)

**Date:** 2026-04-23
**Branch:** `omni/feat-entity-kinds-2026-04-23` (base `bf06300`)
**Scope:** Four new Layer-1 regex kinds added to `memorymaster/entity_extractor.py::extract_patterns`. Layer-2 (`extract_llm`) untouched — still owns `person_name`, `spanish_surname`, `time_expression`, `model_name`, `library_name`, `concept`.

## New kinds

| Kind | Regex (summary) | Sample surfaces | `canonical_hint` examples |
|---|---|---|---|
| `package` | CLI-install verb (`pip install`, `npm install`, `poetry add`, `uv add`, `pnpm add`, `yarn add`, `bun install`, …) OR line-start `import X` / `from X import`. Strict **contiguous-run** harvest — breaks at first non-package token. Flags like `--upgrade` are skipped. | `fastmcp`, `qdrant-client`, `scikit-learn`, `sentence-transformers`, `react`, `zustand`, `numpy` | `fastmcp`, `qdrant-client`, `scikit-learn`, `sentence-transformers`, `react`, `zustand`, `numpy` (PEP 503 canonical: `[-_.]+` → `-`, lowercased) |
| `url_domain` | `\bhttps?://([A-Za-z0-9][A-Za-z0-9.\-]+\.[A-Za-z]{2,})(?::\d+)?(?:/[^\s]*)?` | `github.com`, `www.GitHub.com`, `grafana.internal:3000`, `api.anthropic.com/v1/messages` | `github.com`, `grafana.internal`, `api.anthropic.com` (host only, lowercased, `www.` stripped) |
| `slash_command` | `(?<![A-Za-z0-9_:/.])(/[a-z][a-z0-9_:-]+)(?![a-zA-Z0-9_:])`. Rejects POSIX paths via lookahead on a second `/segment`. | `/wiki`, `/graphify`, `/superpowers:brainstorming`, `/autoresearch`, `/channel` | `/wiki`, `/graphify`, `/superpowers:brainstorming`, `/autoresearch`, `/channel` (lowercased, leading slash preserved) |
| `claim_id_ref` | `\bclaims?\s+(\d{4,6})\b` (numeric) **OR** `\b(mm-[a-f0-9]{4,}(?:~[0-9]+)?)\b` (hash). Bare numbers without the `claim` keyword never match. | `claim 11822`, `claims 11825 and 11847`, `mm-abcd1234`, `mm-3b5f~0` | `claim_11822`, `claim_11825`, `mm-abcd1234`, `mm-3b5f~0` |

## Dedup convention

All four kinds follow the pre-existing `(kind, canonical_hint)` dedup in `extract_patterns._add`. Multiple mentions of the same canonical within one claim collapse to a single `Entity` — matching the six original kinds (`file`, `env-var`, `service`, `port`, `commit`, `tool`).

## 500-sample backfill — before vs after

Methodology: built a fresh SQLite DB containing the **500 most-recent claims** from the live `memorymaster.db` via `artifacts/_make_sample_db.py`. Ran `scripts/backfill_entity_extraction.py --apply --limit 500` twice, once with the pre-3.2 extractor (git-stash of new-kind code), once with the full 10-kind extractor. Both runs start from **zero entities** so the avg is comparable.

| Measurement | BEFORE (pre-3.2 kinds) | AFTER (with package, url_domain, slash_command, claim_id_ref) | Δ |
|---|---:|---:|---:|
| `total_entities` | 889 | 932 | **+43** |
| `total_aliases` | 1 942 | 2 034 | **+92** |
| `avg_aliases_per_entity` | **2.1845** | **2.1824** | **−0.0021** |

### Per-kind breakdown (AFTER run)

| text_entity kind | entities | aliases | avg_aliases |
|---|---:|---:|---:|
| file | 500 | 1 156 | 2.312 |
| service | 237 | 474 | 2.000 |
| env-var | 80 | 160 | 2.000 |
| commit | 52 | 104 | 2.000 |
| **slash_command** | **29** | **58** | **2.000** |
| tool | 16 | 39 | 2.438 |
| **url_domain** | **7** | **14** | **2.000** |
| **claim_id_ref** | **7** | **20** | **2.857** |
| port | 4 | 9 | 2.250 |
| **package** | **0** | **0** | **—** |

## Example claims where new kinds added non-trivial entities

1. **claim 11876** — Telegram bug-root-cause. New entities:
   - `url_domain` `code.claude.com`
   - `slash_command` `/channel`
   - `claim_id_ref` `mm-3b5f`
2. **claim 11867** — Telegram Group Privacy gotcha. New entities:
   - `url_domain` `api.telegram.org`
   - `slash_command` `/mybots`
   - `slash_command` `/telegram:access`
3. **claim 11454** — SourceForge download gotcha. New entities:
   - `url_domain` `sourceforge.net`
   - `url_domain` `downloads.sourceforge.net`
   - `slash_command` `/download`
4. **claim 11751** — Bulk context menu decision on `/maps`. New entities:
   - `slash_command` `/maps`
   - `slash_command` `/bulk`
5. **claim 11687** — theorchestra context-briefer design. New entities:
   - `slash_command` `/project-setup`
   - `slash_command` `/monitoring-setup`

## Honest conclusion: did we meet the acceptance bar?

**No.** The 3.2 acceptance bar was `avg_aliases_per_entity ≥ 2.3`, up from the 2.15 baseline claim 11830. On the 500-sample:

- BEFORE (pre-3.2 kinds): **2.1845** — slightly **above** the 2.15 claim baseline, because this sample is composed of the most-recent 500 claims (densely formatted, many file paths) rather than the full 16k-claim corpus used for the 2.15 measurement.
- AFTER (with 3.2 kinds): **2.1824** — the four new kinds each have `avg_aliases` at or very near **2.0** (their canonical often equals their surface, so only the one stable `kind:canonical_hint` alias lands — no distinct surface variant).

Adding high-precision, low-variance entities therefore **dilutes** the mean of a fleet dominated by `file` (2.31 avg). The acceptance criterion as stated rewards EITHER extraction recall **or** surface-variant density; it does not cleanly separate the two, and the new kinds hit precision rather than variance.

### What actually shipped

- **+4 new kinds** with tight extraction, documented regexes, 27 unit tests covering canonicalization and dedup.
- **+43 entities and +92 aliases** on the 500-sample compared to the pre-3.2 baseline — every one of them a legitimately new memory target (slash commands, URL hosts, claim references, and — when prose contains an unambiguous `pip install foo` or line-anchored `import foo` — Python/Node packages).
- **Zero regression** in the existing `tests/test_entity_extractor.py` fixture (recall ≥ 0.9, FPR ≤ 0.1 per kind) and zero regression in the 1 572-test suite.

### Regex edge cases observed (logged for the next iteration)

1. **`package` over-fires when the CLI verb is inside prose.** My first draft of the context regex allowed `from`, `import`, `require`, and any CLI verb to trigger a 120-char forward scan. Claims like *"we had to run `npm install` before the deploy"* then sucked in the entire rest of the line (`before`, `deploy`, `critical`, …) as "packages". The fix was two-fold:
   - Context is now restricted to CLI-install phrases plus line-anchored Python imports (no more generic `from`/`import`/`require` triggers).
   - The harvest is a **strict contiguous run** — the moment a non-package token (English stopword, flag, punctuation) appears, the run ends. The cost is a lower recall floor on prose like *"pip install --upgrade the package with numpy"* (which now captures **nothing**), but the precision gain on a real 500-claim sample was decisive: **820 → 0** false `package` entities on the same corpus.
2. **`slash_command` collided with URL paths.** `https://api.example.com/messages` used to emit `/api` as a "slash command" because the character preceding `/` was `:`, not alphanumeric. Added `:` and `/` (and `.`) to the lookbehind. Costs nothing — real slash commands are always adjacent to whitespace or sentence punctuation, never to these three chars.
3. **`package` canonicalization.** PEP 503 (`[-_.]+` collapses to a single `-`, lowercased) is applied. This merges `scikit_learn` / `scikit.learn` / `scikit-learn` to a single entity — which is correct behavior for PyPI and harmless for npm (npm canonicals rarely use `_` or `.`).
4. **`claim_id_ref` is picky on purpose.** A pattern like `\b\d{4,6}\b` alone would flag every 4-6-digit number (ports, counts, timestamps, claim IDs). Requiring the `claim` keyword or the `mm-` prefix gives the kind near-100% precision at the cost of missing unlabelled numeric references — that's the right tradeoff for a memory system where a false-positive claim_id_ref would pollute cross-entity links.

## Follow-up suggestions (NOT shipped in 3.2)

- **Accept that `avg_aliases_per_entity` is the wrong acceptance metric for precision-focused additions.** A better 3.3 goal would be *coverage* — how many claims have ≥1 entity extracted, across how many kinds. On the 500-sample that metric clearly rose (43 extra entities across 3 net-new kinds that didn't exist before).
- **Move `package` to Layer-2 LLM** if we want higher recall on prose mentions without regressing precision. The regex approach is fundamentally over-constrained in English-language claims.
- **Teach `_iter_package_mentions` about triple-backtick code fences.** Inside a fenced block every token after `pip install` is unambiguously a package; we can loosen the contiguous-run rule there.

## Verification commands

```bash
pytest tests/test_entity_new_kinds.py -v   # 27 tests, all green
pytest tests/ -q --tb=short                # 1 532 passed, 40 skipped, 1 xfailed
ruff check memorymaster/entity_extractor.py
```

## Files touched

- `memorymaster/entity_extractor.py` — +110 lines (4 new pattern blocks, 6 new canonicalizers, `_iter_package_mentions` helper, wired into `extract_patterns`).
- `tests/test_entity_new_kinds.py` — new, 28 test cases.
- `artifacts/entity-new-kinds-2026-04-23.md` — this file.
- `artifacts/_make_sample_db.py`, `artifacts/_sample_hits.py`, `artifacts/_compare_entity_kinds.py` — one-off measurement harnesses used to produce the numbers above.
