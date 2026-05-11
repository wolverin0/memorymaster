# 0008 Wiki Read Layer and Contradiction Callouts

Date: 2026-05-06

Status: Accepted

Source Claims: claim #9163, claim #36634, claim #36641

## Context

MemoryMaster maintains claims as the write layer and compiles wiki articles as a read layer for humans and agents. Earlier work added bidirectional claim-to-wiki binding so recall output can point back to compiled articles.

Later wiki-layer analysis identified three useful patterns: explicit explored status, inline contradiction callouts, and required sections that expose counter-arguments and data gaps.

## Decision

The wiki remains a compiled read layer over claims. Wiki generation may stamp claim-to-article bindings and expose contradiction information inline.

`wiki_engine.absorb()` deliberately shares contradiction detection with `vault_linter._detect_contradictions()` so read-time wiki output and audit-time lint output agree on what counts as a contradiction.

## Consequences

Users see contradictions while browsing articles, not only when running lint.

Changes to contradiction detection affect both wiki rendering and vault linting. That coupling is intentional and must be tested as a shared behavior.

If contradiction detection is split into deterministic and LLM-verified modes, wiki generation should use the deterministic path because absorb runs regularly.
