# 0011 Recall Ranking Features Stay Measured and Gated

Date: 2026-04-27

Status: Accepted

Source Claims: claim #11898, claim #14546, claim #16864, claim #17229, claim #17285, claim #17418

## Context

MemoryMaster repeatedly tested recall-ranking changes across release cycles. Experiments covered lexical, freshness, graph, claim-type-aware ranking, entity fanout, wiki pointer boosts, structural claim edges, and RRF.

The measurements showed that several plausible improvements were null or harmful on the conversational MemoryMaster corpus, even when they helped on denser benchmarks.

## Decision

New recall-ranking features must remain measured, additive, and gated until they demonstrate lift on the target corpus.

Graph and borrowed recall features default to off when measurements are null or negative. RRF is not a universal default; it is appropriate only when stream topology provides enough populated retrieval streams for reciprocal-rank consensus to help.

## Consequences

MemoryMaster favors honest null results over shipping appealing ranking changes without evidence.

Legacy ranking behavior stays stable unless a feature clears measurement on the relevant corpus.

Future recall work should prioritize real-world recall capture, vector recall, and compaction/dedup before further speculative reranking.
