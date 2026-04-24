# ADR: tokenizer v2 — df=0 IDF penalty + stem/synonym recovery

- **Status:** Accepted
- **Date:** 2026-04-23
- **Commit:** `bb71944` (merge `7d80d83`)
- **Authors:** claude-session + subagent
- **Supersedes:** none (tokenizer v1 had an inverted ranking bug)

## Context

`memorymaster/recall_tokenizer.py` extracts salient tokens from a user prompt before handing them to FTS5. The v1 scorer ranked tokens by smoothed IDF only:

```
idf = math.log((total_docs + 1) / (freq + 1)) + 1
```

When a token had `freq == 0` (no document in the corpus contains it), the formula still returned a finite value — and in fact the *maximum* value in its class (because `(N+1)/1` peaks when `freq=0`). For typical N≈3000 that's IDF ≈ 10.38.

Consequence: typo-riddled prompts, slang, or expletives — tokens that exist nowhere in the corpus — were **promoted** to the top-N "salient tokens" the hook passed to FTS5. FTS5 then searched for garbage. Retrieval recall on real conversational prompts collapsed to 0/30 on the 30-prompt eval (claim 11848 / audit 2026-04-22).

Single-token probes confirmed the corpus itself was healthy (`steward`, `dashboard`, `mergear`, `key rotator` all returned 4–5 hits), so the failure was upstream in token selection.

## Decision

We introduced two changes in tokenizer v2:

1. **df=0 penalty.** Tokens with `df==0` receive an 8.0 penalty on top of their IDF — enough to push them below any df>0 token *unless* whitelisted in a small technical-jargon set (e.g. `afip`, `ci`, `cd`, `db`, `idf`, `lru`, `utf`, `tpm`, `rpm`, `rpd`). The whitelist exists because legitimate low-frequency technical terms otherwise get demoted.
2. **Stem/synonym recovery.** When the original token has df=0 but a morphological stem has df>0 in the corpus, the stem replaces the original and inherits its position.

The penalty magnitude (8.0) was chosen so that in the typical IDF range `[3, 10]`, any df=0 token sinks below any df>0 token. Less would let garbage bubble back up; more is harmless but aesthetically noisy.

Whitelisted technical tokens are explicitly protected from the penalty so short acronyms remain usable even when they happen to be absent from the current corpus snapshot.

## Consequences

- **Measured lift:** non-empty recall rose from 0/30 → 24/30 on the 30-prompt eval, then to 28/30 downstream (commit `7d80d83`, claims 11853 / 11856).
- **p@5 downstream:** 0.197 → 0.280.
- **MAP@5 downstream:** 0.237 → 0.442.
- **Risk:** the whitelist is a hand-curated list; over time it will diverge from the actual corpus. Rebuild when jargon shifts. A future iteration could auto-maintain the whitelist from corpus term frequencies.
- **Non-obvious side effect:** because the penalty is additive, extremely high-IDF tokens (`freq=1` on a huge corpus) still rank first — which is the intended behavior for rare-but-present terms.

## Alternatives considered

- **Drop df=0 tokens entirely.** Rejected because the whitelist set would be lost.
- **Lower the IDF ceiling.** Rejected because it would compress the scale for all tokens, not just garbage.
- **Remove IDF altogether in favor of BM25.** Would not have solved the problem at the token-selection stage — BM25 operates at scoring time, not selection. The BM25 rescorer (commit `159eef7`) is a complementary fix for ranking once candidate documents are fetched; it does not fix token selection.

## References

- Commit `bb71944` / merge `7d80d83`
- `memorymaster/recall_tokenizer.py` lines 76-79 (whitelist), 284-290 (IDF+penalty)
- Claim 11853 (bug root cause), claim 11856 (measured lift)
- Audit `artifacts/retrieval-eval-2026-04-22.md`
