# ARCHITECTURE.md — System Architecture

## Overview
MemoryMaster is a **production-grade memory reliability system** for AI coding agents. It manages "claims" (structured knowledge assertions) with full lifecycle management, citations, conflict detection, steward governance, and MCP integration.

## Core Layers

```
┌──────────────────────────────────────────────────────────┐
│                     Entry Points                          │
│  CLI (cli.py) | MCP Server | Dashboard | LLM Steward     │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│                  MemoryService (service.py)               │
│  ingest | query | run_cycle | dedup | compact | pin       │
└──────┬──────────────────────────────────┬────────────────┘
       │                                  │
┌──────▼───────┐                 ┌────────▼────────────────┐
│  Store Layer │                 │   Jobs Pipeline          │
│  SQLiteStore │                 │  extractor → deterministic│
│  PostgreStore│                 │  → validator → decay     │
│  StoreFactory│                 │  → compactor → dedup     │
└──────┬───────┘                 └─────────────────────────┘
       │
┌──────▼────────────────────────────────────────────────────┐
│              Optional Backends                             │
│  Qdrant (vector) | sentence-transformers (embeddings)      │
└───────────────────────────────────────────────────────────┘
```

## Claim Lifecycle
```
candidate → confirmed → stale → superseded
                              → conflicted → archived
```
- **candidate**: ingested, not yet validated
- **confirmed**: validated with sufficient citations & score
- **stale**: not recently revalidated; confidence decayed
- **conflicted**: contradicting claims detected
- **superseded**: replaced by a newer claim
- **archived**: compacted out; soft-deleted

## Jobs Pipeline (run_cycle)
1. **extractor** — parses claim metadata (type, subject, predicate, object)
2. **deterministic** — rule-based validation (file existence, date checks, etc.)
3. **validator** — citation-count + confidence-score based validation
4. **decay** — time-based confidence decay for stale claims
5. **compactor** — archives old/low-quality claims, prunes events
6. **dedup** — deduplicates near-duplicate claims via embeddings

## Retrieval Modes
- **legacy**: FTS5-based text search + rank_claim_rows
- **hybrid**: semantic vector + lexical + freshness + confidence scoring

## Multi-tenancy
- `tenant_id` column on claims; enforced in service layer
- `MemoryService` can be scoped to a single tenant

## Key Design Decisions
- **Zero mandatory deps** — SQLite works out of the box; everything else is optional
- **Append-only events** — DB triggers enforce immutability of event log
- **Idempotent ingest** — `idempotency_key` prevents duplicate claim creation
- **Policy-driven revalidation** — configurable modes (legacy/prioritized) for candidate selection
