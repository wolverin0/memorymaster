# 0010 Candidate Dedupe Runs Before Validation

Date: 2026-05-01

Status: Accepted

Source Claims: claim #24981, claim #28494, claim #36305

## Context

MemoryMaster accumulated near-duplicate candidate claims. A previous framing suggested dedupe might save a steward LLM call, but `run_cycle` validation uses deterministic and classifier stages rather than an LLM call.

Production verification found real paraphrase pairs and supported promoting dedupe beyond shadow mode.

## Decision

Candidate dedupe is a pre-validator stage in `MemoryService.run_cycle`, after extraction and before deterministic validation. Its value is database cleanup and cleaner recall, not LLM cost reduction.

Dedupe implementations must use unique row identifiers instead of content prefixes. Prefix-based dedupe can produce false negatives.

## Consequences

The validator spends less work on near-duplicate candidates.

Recall quality benefits from less duplicate clutter in confirmed claims.

Precision must remain observable through steward events, and thresholds should become more conservative if false positives are found.
