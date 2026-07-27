# ADR 0015: Governed universal capture lineage
# Covers: the authoritative source-to-evidence-to-claim-to-graph data flow.
# Key terms: source item, evidence item, candidate, confirmed claim, graph support, retirement.
# Read when: adding capture adapters, extractors, forgetting, graph traversal, or provenance UI.
# Status: Accepted on 2026-07-27.
# Decision: graph and retrieval remain derived signals; governed claims remain authoritative.
# Safety: source retirement is logical and candidate promotion remains steward-only.

## Context

Atlas already stores external sources, source items, and evidence. The claim
store already owns lifecycle transitions, citations, scope policy, and trusted
recall. The entity graph already offers a retrieval signal. Without explicit
relational lineage and replay-safe work identities, however, capture producers
can duplicate extraction and graph edges can outlive the claims or sources that
justify them.

## Decision

The canonical flow is:

```text
producer -> source_item -> evidence_item -> candidate claim
         -> steward confirmation -> claim-backed graph support
         -> graph signal -> authoritative claim rehydration -> recall
```

- Producers authenticate and fetch. MemoryMaster accepts content and
  provenance but does not fetch a URL supplied on its own.
- Every accepted capture persists source/evidence before queuing LLM work.
- `claim_evidence_links` is the authoritative claim/evidence lineage.
- `capture_jobs` owns replay identity, leases, retry state, and diagnostics.
- Candidate creation may be automated. Only the steward may confirm a claim.
- `entity_edge_supports` ties each graph relationship to a claim, scope, and
  ontology version. Replaying a claim cannot reinforce an edge twice.
- Trusted graph traversal ignores support from inactive, unauthorized,
  sensitive, or retired-source claims and returns claim/citation explanations.
- `forget` archives a directly targeted claim or logically retires a source.
  It preserves evidence and audit history; privacy erasure remains a separate
  redaction workflow.

## Consequences

The SQLite schema gains additive tables and columns, with Postgres parity and
exact-only backfills. Previous package versions continue to ignore them.
Capture roots and content limits become explicit configuration. The public
facade can stay simple without weakening lifecycle governance.

## Alternatives rejected

- Treat graph/vector output as authoritative: rejected because it bypasses
  lifecycle, scope, sensitivity, and citation checks.
- Fetch arbitrary URLs in MemoryMaster: rejected because authentication,
  SSRF, robots, and source-policy ownership belong to producers.
- Hard-delete on `forget`: rejected because friendly retirement and privacy
  erasure have different audit and safety semantics.
