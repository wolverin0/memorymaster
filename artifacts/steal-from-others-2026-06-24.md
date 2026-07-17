# What to steal from neighboring memory tools — re-survey (2026-06-24)

Refresh of `steal-from-others-2026-04-27.md` (6 months stale). All targets cloned to `cloned/`
(gitignored), changelog-delta read where we had a surveyed version, analyzed fresh otherwise.
Each item: feature → source → why we want it → our gap → effort.

## Corrections to the record (important)
- **gbrain** = `github.com/garrytan/gbrain` (Garry Tan/YC; MIT). No URL was recorded before — now fixed.
- **graphify** = `github.com/safishamsi/graphify` (PyPI `graphifyy`) and **GitNexus** = `github.com/abhigyanpatwari/GitNexus` — the `wolverin0/*` URLs in our docs were **wrong/fabricated**; these are the real upstreams. (Confirmed: both match the tools we use.)
- **My-Brain-Is-Full-Crew** = `github.com/gnekt/My-Brain-Is-Full-Crew` (the `mhss1/MyBrain` first guess was a wrong-match Android app). It IS the multi-platform agent crew — and it's the single most **vision-aligned** project to the user's own Atlas/Jarvis build (see the dedicated section below). Relevant to ATLAS, not MM's memory core.
- **claude-mem relicensed AGPL-3.0 → Apache-2.0 at v13.0.0** — v13+ code is now permissively borrowable (attribution + NOTICE). Pre-v13 stays AGPL.

## TIER S — directly attacks our known-weak retrieval

### 1. Reciprocal Rank Fusion (RRF) for hybrid retrieval  ⭐ CONVERGENT (gbrain + GitNexus both)
**What:** weight-free fusion of FTS5 + vector (+graph) result lists by `1/(k+rank)` instead of a hand-tuned score blend. gbrain (`zerank`+RRF) and GitNexus (BM25+vector RRF) independently use it.
**Why/gap:** MM blends FTS5 + Qdrant + freshness + confidence with an **ad-hoc weighted sum** (the v3.22 boost-floor gate is a patch over exactly this). RRF is the principled, parameter-free standard. Two independent neighbors converging on it is the strongest signal in this survey.
**Effort:** LOW — pure ranking math, ~1 function in the recall ranker; A/B against the current blend on the LongMemEval harness.

### 2. Cross-encoder reranker pass  (gbrain `zerank-2`; Zep added one too)
**What:** after hybrid recall, a cross-encoder re-scores the top-N. gbrain reports it reshuffles ~60% of top-1.
**Why/gap:** MM has **no rerank** — its #1 retrieval failure mode is "agreed-but-wrong / fresh-but-wrong" outranking the true claim (the whole reason the boost-floor gate exists). A rerank pass is the canonical fix.
**Effort:** MED — a rerank step (ZeroEntropy/Cohere/bge-reranker or local); gate behind an env flag; measure R@5/MRR.

### 3. Community detection (Leiden/Louvain) on the entity graph  (graphify, ~270 LOC self-contained)
**What:** cluster the entity graph into topic communities, with **stable size-ranked community IDs** (`remap_communities_to_previous`) and **surprising cross-community connection** scoring.
**Why/gap:** MM proved its GRAPH stream **FLAT** in v3.6 (entity-fanout only, no structure). Community clustering is a *different* way to add structure than call-edges: topic clusters can drive recall boosts, wiki-breakdown, and "related but non-obvious" links (`find_related_claims`). Stable IDs stop wiki articles churning each `run_cycle`.
**Effort:** LOW (drop-in, needs networkx + graspologic) for clustering; MED for the surprise-ranking.

## TIER A — operational quality

### 4. Intent-aware query routing  (gbrain `intent.ts`, deterministic)
Classify query (entity / temporal / event / general) and **toggle ranking knobs** (graph weight, source-boost bypass) off it. MM has `classify_query` but doesn't route ranking from it. **Effort:** LOW-MED.

### 5. Hebbian potentiation + Ebbinghaus decay on graph edges  (MemPalace v3.3.6)
Usage strengthens edges, time decays them — recall-weighted graph dynamics. MM's entity edges are **static**. **Effort:** MED.

### 6. Bitemporal write-time validation  (MemPalace v3.3.5)
Reject inverted intervals (`valid_until < valid_from`) + ISO sanitize **at ingest**. This is MM's exact bitemporal foot-gun (durable-but-invisible rows). **Effort:** SMALL.

### 7. Capability-probed binary resolver + "parseable-response = only success"  (claude-mem v12.6/13.5)
Probe the agent CLI with `--version`, prefer newest, **fail loud** (stale `claude` binary silently killing observations is their bug — and ours waiting to happen). Kill retry counters that mask data loss — matches our own "cynical deletion" lens. **Effort:** SMALL.

### 8. PreToolUse hook intercepting Grep/Glob → inject memory  (codebase-memory-mcp)
Their 99.2%-token-reduction mechanism: intercept the agent's grep/glob and return graph/memory hits as `additionalContext`. MM only injects on prompt (recall hook), not on tool-use. **Effort:** LOW-MED.

### 9. Push / volunteer context  (gbrain v0.42, zero-LLM)
Brain proactively surfaces relevant claims from recent turns (reflex/op/watch channels), confidence-gated, no LLM. MM recall is **pull-only**. **Effort:** MED.

## TIER B — batch / lower ROI

- **`delete_by_source` bulk cleanup (dry-run default) + `checkpoint` batch-ingest** (MemPalace v3.5) — source-scoped purge for eval pollution + one round-trip session filing vs N `ingest_claim`. **SMALL.**
- **Rollup telemetry pattern** (claude-mem v13.5–13.8) — aggregate high-volume events per session/window before forwarding (they cut ~45M→20K events, $7.7k→$10/mo). MM has zero usage visibility. **MED.**
- **Guarded fuzzy resolver** (GitNexus) — entity/symbol linker that **refuses** ambiguous matches (anti-hallucination). **MED.**
- **"Takes vs Facts" epistemology** (gbrain v0.28/0.32) — multi-holder beliefs (take/fact/bet/hunch) with confidence+time; facts fenced as system-of-record. Interesting evolution of our claim model. **HIGH / research.**

## Positioning — the "defined-against" set (mem0 / Letta / Zep / cognee)

The "vector store" strawman is **dead**: mem0 fused BM25+entity, Zep & cognee are temporal/ontology **knowledge graphs**, Letta does agentic self-editing. MM can **no longer** differentiate on "we retrieve/temporal better" — and honestly they're now **ahead** on graph reasoning (cognee's COT/ontology search; Zep's bi-temporal edges). The still-defensible wedge is **governance**: status lifecycle, a steward that validates/decays/dedups, citations, explicit conflict arbitration — **none of the four have it**, and mem0 went the *opposite* way (ADD-only, no overwrite, no conflict resolution). **Reposition: from "we retrieve better" → "we GOVERN what's remembered" (provenance, decay, conflict as first-class). Curation over accumulation.** Adopt their graph-reasoning ideas (above) while keeping governance as the moat.

## My-Brain-Is-Full-Crew (gnekt) — the Atlas mirror (steal for the LifeAgent layer, not MM)

The closest thing to the user's own Atlas/Jarvis vision, already built: **8 role agents** (architect, scribe, sorter, seeker, connector, librarian, postman, transcriber) + **14 skills** (email-triage, inbox-triage, deadline-radar, weekly-agenda, meeting-prep, contact-sync, tag-garden, vault-audit, defrag, deep-clean, transcribe, onboarding, create-agent, manage-agent) over an **Obsidian vault**, with a strict **dispatcher → delegate** router (`DISPATCHER.md`: "never answer directly, only delegate; skills first, agents second") and **4-platform adapters** (claude-code / codex-cli / gemini-cli / opencode via a `.platform/` symlink).

**Steal for ATLAS/Jarvis (not MemoryMaster):**
- **`weekly-agenda` + `deadline-radar` skills** — exactly the time-aware digest we just built; their prompt/skill design is a direct reference. **LOW.**
- **`email-triage` / `inbox-triage`** — a real, structured email-triage skill (we have Gmail flowing now). **MED.**
- **Dispatcher→delegate router** — clean intent-routing pattern for Jarvis (skills-first, agents-second). **LOW-MED.**
- **8-role crew decomposition** — a proven taxonomy for life-agent roles vs Atlas's monolith. **Reference.**
- **4-platform adapter mechanism** — only if Atlas should run beyond Claude+Codex. **Deferred.**

**For MemoryMaster:** little — it's vault-file-based (markdown, no vector/claim engine); its `vault-audit`/`defrag`/`tag-garden` are governance-cleanup *ideas* that echo MM's steward, but MM's engine is more advanced. This one informs the **orchestration** layer, not the memory core.

## Recommended ship plan

**Next patch (≈1 week):** #1 RRF (low, highest-confidence), #6 bitemporal write-guard (small), #7 capability-probed resolver (small).
**Next minor (≈2 weeks):** #2 cross-encoder rerank (the retrieval-quality unlock), #3 community detection (low) + stable IDs.
**Research spikes:** #9 push context, #4 intent routing, #14 takes-vs-facts.

## Non-goals (unchanged + new)
- Don't chase code-structure memory (codebase-memory-mcp / gbrain Cathedral / GitNexus call-graphs) natively — MM already delegates code-intel to GitNexus.
- Don't follow Zep closed-managed or cognee/letta single-Postgres-platform — MM stays single-file SQLite first.
- mem0's ADD-only model is an anti-pattern *for us* (it's the negation of the steward).
