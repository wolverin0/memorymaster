# Recall Eval Expansion — 30 → 100 prompts (2026-04-23)

**Roadmap item:** 1.3 Expand recall eval set.
**Branch:** `omni/feat-eval-expand-100-2026-04-23` (base `3a34b2d`).
**DB under test:** `memorymaster.db` (production snapshot, read-only via `_record_accesses` override).

## Pipeline

1. `scripts/expand_recall_eval.py` walks `~/.claude/projects/G---OneDrive-OneDrive-Desktop-Py-Apps-memorymaster/*.jsonl` (5 session transcripts, 17k+ lines total).
2. Extracts user-role `text` payloads (ignores `tool_result`, `tool_use`, attachments).
3. Filters:
   - Length bounds: 10 ≤ len(text) ≤ 1000.
   - Noise: drops `<system-reminder>`, `<command-name>`, `<local-command-caveat>`, slash-commands (`/clear`, `/continue`, …).
   - **Sensitivity filter (mandatory):** every candidate runs through `memorymaster.security.redact_text`. Any prompt whose redaction differs from the input (or has findings) is dropped.
4. Near-dup rejection: Jaccard over `_candidate_tokens` ≥ 0.8 against the accumulated sample.
5. Seeds with the 30 existing prompts (timestamps/sources preserved) + 10 from `real-prompts-sessionopen.jsonl`, then tops up from transcripts to hit 100.

## Filter funnel

| Stage | Count |
|-------|-------|
| Scanned user prompts (transcripts) | 515 |
| Passed length + noise + sensitivity filter | 64 |
| Passed near-dup (post-seed) | 61 |
| **Final sample** | **100** (30 existing + 70 new) |

Reject reasons (top): `too_long`=6, `noise`=5, `near_dup`=3, `sensitive:google_api_key+prose_password`=1.

## Ground-truth labelling (heuristic, LLM-free)

For each of the 70 new prompts, the hook recall pipeline runs with `MEMORYMASTER_RECALL_VERBATIM=0`, entity fan-out ON, vector fallback OFF, top-K=20. A candidate claim is marked *relevant* iff:

- its `(subject + text)` shares **≥ 3 content tokens** with the prompt (same stopword/stem filter as the recall tokenizer), AND
- `status NOT IN ('stale', 'archived')`.

Output: `artifacts/real-prompts-100-labels.json` (`{prompt_sha → [claim_id,...]}`).

- Mean relevant claims / new prompt: **2.13**
- New prompts with **0** heuristic-relevant claims: **42 / 70**
- Spot-check **recommended** (~10 prompts); these labels are coarser than the token-overlap≥2 proxy the eval harness uses at scoring time, so they are documentation, not truth — the eval scripts still compute their own labels per run.

## Baseline table (`MEMORYMASTER_RECALL_VERBATIM=0`, `FUSION=linear`, `w0=(0.3,0.3,0.2,0.3,0.1,0,0,0)`, `min_overlap=2`)

| Eval | Prompt set | Size | p@5 | MAP@5 | non_empty (top-5 ≥1 hit) |
|------|------------|------|-----|-------|--------------------------|
| precision_at_5 | real-prompts.jsonl | 30  | 0.313 | 0.473 | 17/30 (56.7%) |
| precision_at_5 | real-prompts-100.jsonl | 100 | 0.358 | 0.500 | 67/100 (67.0%) |
| quality        | real-prompts.jsonl | 30  | — | — | AFTER 29/30 (96.7%) [before 2/30] |
| quality        | real-prompts-100.jsonl | 100 | — | — | AFTER 99/100 (99.0%) [before 2/100] |

Observations:

- **p@5 and MAP@5 both rose slightly** on the 100-prompt set (+0.045 / +0.027). The 30-prompt set was dominated by short Spanish instructions with 1–2 discriminating tokens; the 70 new prompts span more English/technical tasks that the tokenizer + entity fan-out already cover well.
- **Candidate fan-out** (`mean candidates/prompt`) rose from 18.6 → 21.9, meaning many new prompts are richer and produce fuller top-20 pools.
- **quality harness** confirms the tokenizer lift scales: after-fan-out hit-rate is 99% on the 100-set vs 6.7%/2.0% raw-FTS5 before-rate. Raw-FTS5 baseline drops slightly on the 100-set because several new prompts are quote-heavy or include command fragments that don't parse as a single clean FTS5 query.

## 3 sample new prompts with heuristic-relevant labels

1. **relevant=[10631, 10665]** — "im having an issue with my windows account @123.png @1234.png this appears everytime i restart windows and my windows store apps dont work, are broken…"
2. **relevant=[11850, 9709, 9735, 9705, 9717]** — "i lost this project session because of corruption, i need to grab it back, this was a implementation try of this project https://github.com/ruvnet/RuView…"
3. **relevant=[10619, 10640, 9180, 8722]** — "You are agent 01a7aa5a-6dd0-41d4-b140-41dca883663e (NetworkAnalyst). Continue your Paperclip work." (agent-wake prompt — heuristic finds cross-project Paperclip/agent claims.)

## Reproducibility

```bash
# Rebuild the 100-prompt set (re-runs labelling)
python scripts/expand_recall_eval.py

# Rerun baselines
MEMORYMASTER_RECALL_VERBATIM=0 python scripts/eval_recall_precision_at_5.py --prompts artifacts/real-prompts.jsonl
MEMORYMASTER_RECALL_VERBATIM=0 python scripts/eval_recall_precision_at_5.py --prompts artifacts/real-prompts-100.jsonl
python scripts/eval_recall_quality.py --prompts artifacts/real-prompts.jsonl
python scripts/eval_recall_quality.py --prompts artifacts/real-prompts-100.jsonl
```

## Known gotchas (carried forward)

- Claim 11855 / 11882: eval harness does NOT run the live BM25 rescorer — uses raw FTS5 `lexical_score`. Absolute numbers can diverge from prod, but A/B weight comparisons remain valid.
- The 70-new-prompt heuristic labels are a side-car for documentation/debugging only; the eval harness computes its own token-overlap labels per run (`--min-overlap=2` default), so no coupling between the side-car and the baseline numbers above.
