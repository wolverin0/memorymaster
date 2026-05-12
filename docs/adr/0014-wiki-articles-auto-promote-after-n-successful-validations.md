# 0014 Wiki Articles Auto-Promote After N Successful Validations

Date: 2026-05-12

Status: Accepted

Source: PR #47 (`8924cd5`)

## Context

MemoryMaster's wiki is the compiled-truth read layer over the claims write layer. The wiki engine explicitly follows the Karpathy/Farza article pattern: grouped by theme rather than chronology, with compiled truth above a timeline section (`memorymaster/wiki_engine.py:1`, `memorymaster/wiki_engine.py:23`, `memorymaster/wiki_engine.py:34`).

Before this decision, wiki promotion was an explicit absorb operation over claim groups. `absorb()` loads claims by topic and writes or updates subject articles in batch (`memorymaster/wiki_engine.py:307`, `memorymaster/wiki_engine.py:317`, `memorymaster/wiki_engine.py:362`, `memorymaster/wiki_engine.py:414`).

For this decision, a "validation" is a claim lifecycle event with `event_type == "validator"`. The validator job passes that event type when it transitions claims to superseded, stale, conflicted, or confirmed states (`memorymaster/jobs/validator.py:110`, `memorymaster/jobs/validator.py:132`, `memorymaster/jobs/validator.py:163`, `memorymaster/jobs/validator.py:184`). `transition_claim()` applies the status transition, then evaluates wiki auto-promotion for validator events (`memorymaster/lifecycle.py:64`, `memorymaster/lifecycle.py:71`).

## Decision

Wiki articles auto-promote after N successful validator lifecycle events for a claim, where N is configurable by `MEMORYMASTER_WIKI_AUTOPROMOTE_THRESHOLD` and currently defaults to `3` (`memorymaster/lifecycle.py:24`, `memorymaster/lifecycle.py:27`). Invalid threshold values are ignored, and values less than or equal to zero disable auto-promotion (`memorymaster/lifecycle.py:28`, `memorymaster/lifecycle.py:33`).

The trigger runs only for validator events (`memorymaster/lifecycle.py:25`). It lists recent validator events for the claim, requests one more than the threshold, and promotes only when the number of distinct validator event IDs is exactly the threshold (`memorymaster/lifecycle.py:36`, `memorymaster/lifecycle.py:38`). That exact-count check makes the threshold crossing the promotion point instead of repeatedly promoting on every later validation.

The side effect is a call to `absorb_single_claim()` using the store's database path (`memorymaster/lifecycle.py:40`, `memorymaster/lifecycle.py:42`). `absorb_single_claim()` selects the target claim if it is confirmed or candidate, gathers sibling claims for the same subject and scope, writes or updates the subject article, stamps the claim-to-wiki binding, refreshes backlinks, and returns the article metadata (`memorymaster/wiki_engine.py:452`, `memorymaster/wiki_engine.py:472`, `memorymaster/wiki_engine.py:493`, `memorymaster/wiki_engine.py:573`, `memorymaster/wiki_engine.py:577`, `memorymaster/wiki_engine.py:578`, `memorymaster/wiki_engine.py:579`).

Failures in the auto-promotion path are logged and do not abort the lifecycle transition (`memorymaster/lifecycle.py:43`).

## Alternatives Considered

Manual promote only was rejected because compiled truth would lag behind the steward and require operators to remember a separate wiki step.

Immediate promote on first ingest was rejected because initial ingestion is too noisy; article generation should wait for repeated validator signal.

Time-based promotion after T days was rejected because elapsed time is weaker evidence than actual validator events and can drift from claim quality.

N validations was accepted because it ties wiki promotion to repeated steward signal while keeping the threshold configurable and disableable.

## Consequences

The positive consequence is that compiled truth keeps pace with steward validation without requiring a separate manual absorb cycle for every validated claim.

The negative consequence is that false positives can enter the wiki if the steward over-validates the same claim or if validator events are emitted too broadly.

The main tradeoff is freshness versus noise. A lower threshold updates wiki articles sooner; a higher threshold waits for more validator evidence. Deployments can tune or disable the behavior through `MEMORYMASTER_WIKI_AUTOPROMOTE_THRESHOLD` (`memorymaster/lifecycle.py:27`, `memorymaster/lifecycle.py:33`).

## Implementation References

- `memorymaster/lifecycle.py:24` defines `_wiki_autopromote_after_validator`.
- `memorymaster/lifecycle.py:27` sets the default threshold to `3`.
- `memorymaster/lifecycle.py:36` counts validator events for the claim.
- `memorymaster/lifecycle.py:38` promotes only on the exact threshold crossing.
- `memorymaster/lifecycle.py:71` invokes auto-promotion after status transition.
- `memorymaster/wiki_engine.py:452` defines `absorb_single_claim`.
- `memorymaster/wiki_engine.py:573` writes or updates the wiki article.
- `memorymaster/wiki_engine.py:577` stamps the claim-to-wiki binding.
- `tests/test_wiki_autopromote.py:41` covers promotion after the third validator event.
- `tests/test_wiki_autopromote.py:65` covers threshold `0` disabling auto-promotion.
