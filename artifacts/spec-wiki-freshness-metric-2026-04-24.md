# Spec — Wiki absorb freshness metric

**Status:** draft, USER-INPUT (product decision pending).
**Author:** claude-session, 2026-04-24.
**Context:** Roadmap item 9.2 flagged USER-INPUT because "fresh" is a product decision.

---

## Problem

`wiki-absorb` compiles claims into obsidian-vault/wiki articles with a truth section + append-only timeline. Over time:

- Some articles absorb new claims weekly → truth + timeline both current.
- Some articles haven't absorbed anything in >30 days — either because the topic is stable (desired) or because the claim stream dried up and we're looking at stale truth (undesired).
- Some articles absorbed a flurry of claims months ago and went silent — looks stale but the compiled truth might still be accurate.

There is currently no metric that lets a human-or-agent answer "is this article fresh?" without hand-reading.

## What "fresh" could mean (4 options)

Each option has a different shape, a different DB cost, and a different failure mode.

### Option A — Absorption recency
**Metric:** days since the article's most recent absorb event.
**Signal:** `max(events.created_at WHERE event.article == <article> AND event.kind == 'wiki_absorb')`.
**Pro:** cheap, precise, already trackable via `events` table.
**Con:** articles describing stable-but-important facts (e.g. "PostgreSQL is a relational database") look stale forever.

### Option B — Claim turnover rate
**Metric:** fraction of the article's supporting claims whose `last_validated_at` is within N days.
**Signal:** `COUNT(claims WHERE claim.wiki_article == <article> AND claim.last_validated_at > now - Ndays) / COUNT(claims WHERE claim.wiki_article == <article>)`.
**Pro:** distinguishes "stable-and-still-true" (high turnover = high freshness) from "stale and untouched" (no recent validation).
**Con:** requires claims to be validated as part of steward cycle — today `last_validated_at` is populated inconsistently.

### Option C — Contradiction pressure
**Metric:** count of claims referencing entities/subjects in this article whose `status` is `conflicted` or `candidate` (not yet resolved).
**Signal:** join `claims` + `entity_aliases` + filter on status.
**Pro:** actively surfaces articles where new information is disagreeing with compiled truth — the most operationally useful signal.
**Con:** expensive (multi-join per article); only works after entity-extraction is ubiquitous.

### Option D — Retrieval traffic
**Metric:** number of recall-hook hits per article in the last N days.
**Signal:** requires adding a counter to `context_hook.py::recall` that increments per article surfaced.
**Pro:** "fresh" = "currently load-bearing." Directly ties freshness to operational value.
**Con:** new instrumentation needed; articles covering rare-but-critical topics look stale.

## Composite proposal

Weight all four:

```
freshness(article) =
    0.25 * exp(-days_since_last_absorb / 30)        # A
  + 0.25 * claim_turnover_30d                        # B
  + 0.25 * (1 / (1 + open_conflicts_on_article))     # C
  + 0.25 * log1p(recall_hits_30d) / log1p(max_hits)  # D
```

Clamp to [0, 1]. Threshold suggestions:

| freshness | Interpretation | Action |
|---|---|---|
| ≥ 0.7 | Current and load-bearing | Leave alone |
| 0.4 – 0.7 | Mixed signals | Candidate for `wiki-cleanup` |
| < 0.4 | Stale and/or disconnected | Either re-absorb, archive, or flag for human review |

## Open decisions for the user

1. Is the four-signal composite the right shape, or should we ship a single-signal metric (A or C)?
2. Should freshness be a per-article number or a per-section number (truth / timeline)?
3. Where does it surface? Options:
   - `lint-vault` as a new warning class
   - New `wiki-freshness` CLI command producing a sorted table
   - Column in the generated `obsidian-vault/bases/*.base` views
4. What's the action trigger? Automatic wiki-cleanup on `freshness < 0.4`, or advisory only?
5. Cadence: per-steward-cycle, nightly, or on-demand?

## Implementation sketch (once shape is agreed)

- New column on the wiki article's persisted summary (if any) or a cache file `obsidian-vault/wiki/.freshness.json`.
- New CLI subcommand `python -m memorymaster ... wiki-freshness [--threshold 0.4]`.
- New optional field in `bases-generate` output.
- Tests in `tests/test_wiki_freshness.py` with fixture articles spanning all four signals.

## Estimate

- Single-signal (A only): 0.5 day.
- Composite with C + D: 2–3 days (needs claim status sweep + recall instrumentation).

## Why this stays USER-INPUT

Picking a single-signal metric that doesn't match your actual mental model of "fresh" is worse than no metric — you'll stop trusting the signal and the added complexity pays no dividends. Ship only after the shape is agreed.
