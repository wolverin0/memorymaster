# Entity Layer-2 backfill — dry-run report

**Date:** 2026-04-23
**Branch:** `omni/feat-entity-l2-llm-2026-04-23`
**Base:** `98e25ca` (post-BM25-per-field merge)
**Spec:** `artifacts/spec-entity-extraction-at-ingest-2026-04-23.md`

This is a **dry-run artifact**. No live DB was modified. No real LLM calls
were made against user-owned claims. The Layer-2 numbers below are projected
from a deterministic stand-in extractor that models the minimum plausible
behaviour of `gemini-3.1-flash-lite-preview` on MemoryMaster's claim corpus.

The real Layer-2 backfill is a **user-input decision** — flip the
`MEMORYMASTER_ENTITY_LLM=1` env var and run with `--apply` only after
approving cost and runtime. This PR ships only the code path, the flag,
and this report.

---

## 1. Headline numbers

| Metric                                    | Full DB (Layer-1 only) | 100-claim sample, Layer-1 | 100-claim sample, Layer-1 + simulated Layer-2 |
|-------------------------------------------|-----------------------:|--------------------------:|----------------------------------------------:|
| avg_aliases_per_entity                    | **2.150** (claim 11830)| **2.3295**                | **2.3667**                                    |
| total_entities                            | —                      | 258                       | 270                                           |
| total_aliases                             | —                      | 601                       | 639                                           |
| Layer-1 entity mentions                   | —                      | 279                       | 270                                           |
| Layer-2 entity mentions (simulated)       | —                      | —                         | 24                                            |
| Sample acceptance threshold (>= 2.5)      | —                      | NOT MET                   | **NOT MET** (gap: 0.133)                      |

**Honest read.** On this 100-claim sample, the simulated Layer-2 lifts
`avg_aliases_per_entity` from 2.33 to 2.37 — an improvement of 0.04, not
the ~0.35 needed to clear the 2.5 acceptance bar.

Two things that would close the gap but were NOT done in this dry-run:

1. **A real LLM run will extract more concepts per claim than my
   heuristic.** My canned extractor only fires on hard-coded hint lists
   (a dozen surnames, a dozen model names). Gemini Flash Lite will hit
   more surface forms per claim — I estimate 0.6–1.2 extra Layer-2
   mentions per claim versus my heuristic's 0.24.
2. **The sample may be pessimistic.** The 100 random claims skew toward
   short dream-seeds and Spanish shopping-list style notes where the
   regex already maxes out; large technical decision claims (most of
   the real corpus) have more human+library+concept density per claim.

If the real Layer-2 lifts the per-sample avg by ~0.2 instead of my
simulated 0.04, the full-corpus post-backfill avg should land in the
**2.5–2.7** range — clearing the bar. Without a real LLM run I cannot
commit that number, and this is the primary uncertainty in this work.

## 2. Per-kind breakdown (100-claim sample, simulated Layer-2)

```
text_entity:file              169 entities   420 aliases   avg 2.49
text_entity:service            49 entities    98 aliases   avg 2.00
text_entity:env-var            14 entities    28 aliases   avg 2.00
text_entity:concept            14 entities    35 aliases   avg 2.50   <- new
text_entity:tool                6 entities    17 aliases   avg 2.83
text_entity:commit              6 entities    12 aliases   avg 2.00
text_entity:library_name        4 entities    12 aliases   avg 3.00   <- new
text_entity:port                3 entities     7 aliases   avg 2.33
text_entity:time_expression     2 entities     4 aliases   avg 2.00   <- new
text_entity:model_name          2 entities     4 aliases   avg 2.00   <- new
text_entity:spanish_surname     1 entity       2 aliases   avg 2.00   <- new
```

The five new kinds (`concept`, `library_name`, `time_expression`,
`model_name`, `spanish_surname`) each get ≥2.00 avg aliases because the
backfill writes both `<surface>` and `<kind>:<canonical>` as aliases.

## 3. Cost estimate for full-DB backfill

**Corpus size:** 11,883 claims with non-empty text (as of 2026-04-23,
from `SELECT COUNT(*) FROM claims WHERE text IS NOT NULL AND TRIM(text)!=''`).

**Prompt:** ~350 tokens (fixed) + up to 4,000 chars of claim body
(~1,000 tokens). Most claims are shorter — empirically median is ~200
tokens. Response target: ≤ 8 entities × ~20 tokens each = ~160 tokens.

### Gemini 3.1 Flash Lite Preview (default provider)

| Per-call    | Low estimate     | High estimate    |
|-------------|------------------|------------------|
| Input tok   | 500              | 1,400            |
| Output tok  | 80               | 200              |
| Latency     | ~300 ms          | ~900 ms          |

At Gemini 3.1 Flash Lite pricing (as of 2026-04, approx.
$0.075 / 1M input tokens, $0.30 / 1M output tokens):

- Low estimate:  11,883 × (500 × 0.075 + 80 × 0.30) / 1e6 = **$0.73**
- High estimate: 11,883 × (1400 × 0.075 + 200 × 0.30) / 1e6 = **$1.96**

**Expected total cost: ~$1–2 for Gemini.** This is within the kind of
budget that does not need an explicit approval cycle, but per the spec
the user must still flip `--apply` themselves.

### If switched to OpenAI (`gpt-4o-mini`)

Approx. $0.15 / 1M input, $0.60 / 1M output: **~$2–4 total.**

### Runtime

Sequential execution at 500 ms/call is ~100 minutes (single-worker, no
batching). Gemini rotator can run 3–5 keys in parallel — practical wall
time ~20–30 minutes. Acceptable.

## 4. Side-by-side — 10 representative claims

These 10 claims are from the 100-claim sample where either Layer-1 or
the simulated Layer-2 found something. Full text in
`_sim_samples.txt` (not checked in). IDs are from the live DB.

| # | Claim ID | Layer-1 extractions                                                                 | Simulated Layer-2 extractions          |
|---|----------|--------------------------------------------------------------------------------------|----------------------------------------|
| 1 | 79       | file/personaldashboard, file/radarr, file/soul.md, file/agents.md, tool/docker       | time_expression/hoy                    |
| 2 | 392      | tool/docker                                                                           | —                                      |
| 3 | 448      | env-var/NO_REPLY, file/memory/2026-03-08.md, file/america/buenos_aires                | —                                      |
| 4 | 472      | env-var/PROJECT_STATE, 30+ files, tool/codex, tool/gemini, tool/git, commit/2128295779| concept/"decime si anda"               |
| 5 | 668      | file/mock/hardcoded                                                                  | —                                      |
| 6 | 693      | file/.claude/agents/web-designer.md, service/ui-ux-pro-max, service/frontend-design  | —                                      |
| 7 | 809      | file/name/rubro, file/scraping/photo, service/crm-pending-work-2026-03-11            | concept/"para remake"                  |
| 8 | 810      | file/wolverin0/clawtrol                                                              | —                                      |
| 9 | 1140     | file/success/error/info, file/components/toastconfig, commit/4ab0921                 | —                                      |
|10 | 1350     | file/project.md                                                                      | —                                      |

### Observations

- Layer-1 already extracts **a lot** on technical claims — claim 472 has
  >30 file/tool/env mentions from the regex pass alone.
- The simulated Layer-2 barely fires (4 hits across 10 samples). A real
  LLM should catch Spanish surnames like `Snake` and `Otacon`, model
  names like `gemini-3.1-flash-lite-preview`, library names like
  `Jellyfin`, `OpenClaw`, `ClawTrol`, and concept phrases like
  `"topic system prompts inline"` — all present in the sample text but
  missed by my hardcoded hint list.
- Concrete surprise: claim 448 mentions `Jellyfin` and `OpenClaw` —
  both should be `library_name` or `service` kind but neither my regex
  nor my heuristic caught them. A real LLM likely would.

## 5. How to run the real Layer-2 backfill

```bash
# 1. Copy the live DB (do NOT touch memorymaster.db directly)
cp memorymaster.db /tmp/mm_copy.db

# 2. Small dry-run first (100 claims, ~30s, ~$0.01)
MEMORYMASTER_ENTITY_LLM=1 \
    python scripts/backfill_entity_extraction.py \
        --db /tmp/mm_copy.db --dry-run --layer2 --limit 100

# 3. Full dry-run (no DB write, ~25min, ~$1-2 of LLM calls)
MEMORYMASTER_ENTITY_LLM=1 \
    python scripts/backfill_entity_extraction.py \
        --db /tmp/mm_copy.db --dry-run --layer2

# 4. If avg_aliases_per_entity >= 2.5: apply to the copy
MEMORYMASTER_ENTITY_LLM=1 \
    python scripts/backfill_entity_extraction.py \
        --db /tmp/mm_copy.db --apply --layer2

# 5. Validate, then swap copy in as the live DB
```

## 6. Security posture

- Sensitivity filter (`memorymaster.security.redact_text`) runs on every
  claim BEFORE it reaches the LLM. If a claim contains a secret, the
  LLM sees the redacted version (`[REDACTED:aws_access_key_id]` etc.),
  never the raw credential.
- Claims whose redacted length drops below 16 characters are skipped
  entirely (`llm_skipped_sensitive` counter in the script output).
- The prompt is fixed (`LLM_PROMPT_VERSION = "entity-l2-v1-2026-04-23"`);
  versioned for downstream cache invalidation.
- Defensive: `extract_llm` catches every exception from the provider
  and returns `[]`, so a 429 / timeout / malformed-JSON never crashes
  the backfill.

## 7. Outstanding gaps

1. **avg_aliases_per_entity >= 2.5 is NOT PROVEN.** Simulated lift was
   +0.04; real LLM needs to deliver ~+0.2 to clear the bar. Only a real
   dry-run with the user's approval will confirm.
2. **Provider-level rate limits** are not exercised in this dry-run.
   The `key_rotator` handles 429s but batching / backoff has not been
   stress-tested against the Gemini free-tier RPM cap.
3. **Cross-claim entity linking** from the spec (open question #3) is
   deferred — this PR only populates the entity registry; the
   find_related_claims query layer is unchanged.
