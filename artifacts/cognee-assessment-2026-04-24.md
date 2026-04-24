# Cognee vs MemoryMaster — assessment (2026-04-24)

**Source:** Akshay Pachaar's X thread 2026-04-13 ([x.com/akshay_pachaar/status/2043745099792953508](https://x.com/akshay_pachaar/status/2043745099792953508)) pitching [Cognee](https://github.com/topoteretes/cognee) (open-source agent memory engine).

**Claim 11899** captures the reference.

---

## What Cognee is

A three-store agent memory engine behind four async calls:

```python
await cognee.add(doc)        # ingest
await cognee.cognify()       # extract entities/relationships, dedup, dual-index
await cognee.memify()        # RL-ish pass: strengthen useful paths, prune stale
await cognee.search(query)   # 14 retrieval modes
```

Default stack: SQLite + LanceDB + Kuzu (embedded, file-based). Production: swap to Postgres + Qdrant/Pinecone + Neo4j/FalkorDB.

**Three stores, three dimensions:**
- Relational — provenance (where/when/who)
- Vector — semantics (what content means)
- Graph — relationships (how entities connect)

## Side-by-side with MemoryMaster

| Concern | Cognee | MemoryMaster (today) |
|---|---|---|
| Relational/provenance | SQLite with `cognify` pipeline | SQLite + citations + events (mature) |
| Vector search | LanceDB/Qdrant/Pinecone embedded | Qdrant fallback (opt-in, `W_VECTOR`) |
| Graph/relationships | Kuzu/Neo4j/FalkorDB first-class | **No graph store** — entity_aliases table is flat |
| Entity extraction | LLM in `cognify`, dedup by content hash | L1 regex (claim 11830) + L2 LLM stub (claim 11885) |
| Self-improving memory | `memify` RL loop — edge-weight tuning, path strengthening | **None** — steward classifier is static post-training; tier recompute + dedup don't learn from retrieval success |
| Retrieval | 14 modes, graph-completion default | 5 streams (BM25, entity fanout, vector, verbatim, freshness) + 8-dim linear / RRF fusion |
| Multi-tenancy | Graph-level dataset permissions | scope string + visibility flag |
| Session memory / pronoun resolution | Built in (`session_id` on search) | Not explicit — relies on prompt context |

## What MemoryMaster is already doing well

1. **Lifecycle + bitemporal model** — claims have status transitions (candidate→confirmed/stale/superseded/conflicted/archived), `event_time`/`valid_from`/`valid_until`, supersedes links. Cognee doesn't advertise this level of temporal modeling.
2. **Classifier-driven promotion** — v2/v3 classifiers with calibrated probability thresholds (commits `6679805`, `8ee84cb`, claims 11831, 11894). Closest Cognee analog is `memify` but its edge-weight tuning is less interpretable.
3. **Wiki layer** — compiled truth + timeline articles with frontmatter schema and a lint hook. Cognee has no equivalent to the wiki-absorb / compiled-article flow.
4. **Sensitivity filter** — v1 + v2 adversarial corpora (300 samples, F1 ≥ 0.995; claim 11886). Cognee does not pretend to handle secret redaction.
5. **Latency instrumentation** — per-stream p50/p99 (claim 11887). Cognee benchmarks are aspirational on their README.

## The real gap Cognee highlights

**Multi-hop queries.** Cognee's canonical example — "Alice is tech lead on Atlas" + "Atlas uses PostgreSQL" + "PostgreSQL outage Tuesday" → "was Alice's project affected?" — is exactly the failure mode LongMemEval surfaces:

- Claim 11896: **100% of LongMemEval hit@5 misses have their top-1 from another question's seeded claims.** Cross-question contamination. Our retrieval does not traverse relationships.
- Claim 11898: RRF wins on LongMemEval (+18% hit@1) because the benchmark's dense per-Q seeding mimics the multi-stream environment RRF was designed for. But we still don't have a *relationship* stream.

Graph traversal would close this specifically. BM25 + vector + entity-alias fanout are all *similarity* mechanisms — none of them follow edges.

## Risks of adopting Cognee

1. **New hard dependencies.** Cognee pulls in Kuzu (or Neo4j/FalkorDB at scale). That's a platform binary to install, version-pin, and maintain on Windows + Linux CI. MemoryMaster's current appeal is "single-file SQLite, no server."
2. **Overlap, not replacement.** Cognee's `add`/`cognify` pipeline would duplicate our `ingest_claim` + sensitivity filter + lifecycle machinery. We'd have two ingest paths to keep in sync — or abandon one.
3. **Sensitivity filter regression.** Cognee does not run a claims-grade sensitivity filter. If we use Cognee as a transparent ingest path, secrets leak. We would have to wrap `cognee.add` with our `redact_text` — losing the "four calls" simplicity they advertise.
4. **Memify opacity.** The RL edge-weight loop is an automated rebalancer. Our steward classifier is interpretable (we can see feature weights). Trading that for an opaque learner is a governance step back on a project whose whole point is auditable memory.
5. **Wiki conflict.** Our compiled-truth wiki articles are a human-facing artifact with hand-curated frontmatter. Cognee has no corresponding concept — integration would either double-write or pick a winner.

## Recommendation

**Do NOT replace MemoryMaster with Cognee.** Our lifecycle/bitemporal/classifier/wiki layer is the differentiator, and Cognee lacks all four.

**Do consider Cognee's graph idea as an opt-in retrieval stream.** Specifically:
- Add a **Kuzu-backed graph stream** (6th stream, gated by `MEMORYMASTER_RECALL_GRAPH=1`), populated during the existing entity-extraction path — each entity becomes a node, each claim-mentions-entity becomes an edge.
- Surface a single graph-traversal retrieval on `recall()` — "claims within N hops of entities in the query."
- Weight via `W_GRAPH` alongside W_ENTITY/W_VECTOR/etc. Claims 11881 + 11898 suggest RRF will benefit once graph is a real stream (going from 2 populated streams to 3 on LongMemEval is exactly the condition where RRF pulled ahead of linear).
- Skip `memify` entirely for now. Our steward classifier + tier recompute cover the same surface with better interpretability.

**Concrete next spec (if approved):** new file `artifacts/spec-graph-retrieval-stream-2026-04-24.md` sketching the Kuzu-as-third-backend integration, ~1 week of work. Acceptance: LongMemEval hit@5 lift ≥ 0.05 over current linear+BM25 baseline.

## One thing to steal right now (no code changes)

The article's **7 failure modes of memory-less agents** (context amnesia / zero personalization / multi-step task failure / repeated mistakes / no knowledge accumulation / hallucination from gaps / identity collapse) is a sharper framing than our current docs. Worth adding to `docs/recall-architecture-2026-04-23.md` as a "what MemoryMaster is solving" preface.

## Claims referenced

- 11830 (L1 entity extraction shipped)
- 11885 (L2 LLM stub honest null)
- 11896 (LongMemEval cross-Q contamination is the sole hit@5-miss cause)
- 11898 (RRF is stream-topology-dependent)
- 11899 (this reference)
