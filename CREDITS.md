# Credits, Prior Art & Re-Survey Watchlist

MemoryMaster is a **synthesis**. The core thesis — memory as *governed, lifecycle-managed
claims with citations* (candidate → confirmed → stale → superseded → conflicted → archived),
a steward that validates/decays/dedups, and a sensitivity filter at ingest — is its own. But
much of the retrieval, entity-detection, wiki, cross-repo-graph, and local-search machinery was
**magpie'd, with thanks, from neighboring projects**. This file gives them the visible nod they
deserve, and doubles as a **re-survey watchlist** so we periodically steal what's new.

> The original survey (`artifacts/steal-from-others-2026-04-27.md`) is from **2026-04-27**.
> These projects ship fast — treat anything here older than ~3 months as stale and worth a re-read.

---

## 1. Borrowed features (the "steal everything good" survey — v3.9.0)

| Project | What we took |
|---|---|
| **gbrain** ("Code Cathedral") | two-pass retrieval, structural call-graph edges, parent-scope chunking, source-aware ranking, frontmatter-guard CLI |
| **MemPalace** | "Closets" (two-tier search pointers), "Halls" → `claim_type`-aware ranking, entity-detection overhaul (CamelCase + git-author entities), `cwd`-from-transcript scope derivation |
| **graphify** | `merge-graphs` / cross-repo knowledge graphs → our `federated_query` |
| **claude-mem** | multi-account isolation (per-UID worker ports), the "cynical deletion" audit philosophy (kill *defenders* & *tolerators*) |
| **My-Brain-Is-Full-Crew** | multi-platform adapter pattern (one source → Claude / Codex / Gemini / OpenCode) |
| **GitNexus** | cross-repo impact routing; also the code-intelligence layer MemoryMaster itself runs on |

## 2. Systems we define ourselves *against* (design positioning)

- **mem0**, **Letta / MemGPT**, **Zep** — vector-store memory. MM's whole pitch ("govern claims,
  don't just store more embeddings") is a deliberate reaction to these. See README → *How it's different*.
- **cognee** — evaluated in `artifacts/cognee-assessment-2026-04-24.md`.

## 3. Patterns & concepts we build on

- **Karpathy / Farza "LLM Wiki"** — the compiled-truth + append-only-timeline wiki engine.
- **Voidtools Everything (`ES.exe`)** — the v4.1.0 local-filesystem bridge (`resolve-project` / `local-search`).
- **LongMemEval-S** + **agentmemory** — the benchmark + the peer we measure R@5 / MRR against.
- **Keep a Changelog** + **SemVer** — release discipline.

## 4. Stands on / integrates with

Obsidian (wiki vault) · Claude Auto Dream (Dream Bridge sync) · Qdrant / SQLite FTS5 / Postgres + pgvector (backends) · MCP / FastMCP (protocol).

---

## 5. Re-survey watchlist — DUE (survey is 6+ months old)

**Re-read the v3.9 six for new releases** (gbrain, MemPalace, graphify, claude-mem, My-Brain-Is-Full-Crew, GitNexus) — many features above were partials; check what they shipped since 2026-04.

### Re-survey COMPLETED 2026-06-24 → full detail in `artifacts/steal-from-others-2026-06-24.md` (all 12 cloned to `cloned/`)

- **Corrected upstreams** (our docs had wrong/fabricated URLs): gbrain = `garrytan/gbrain` (was unrecorded), graphify = `safishamsi/graphify`, GitNexus = `abhigyanpatwari/GitNexus` (the `wolverin0/*` URLs were fabricated), My-Brain-Is-Full-Crew = `gnekt/My-Brain-Is-Full-Crew`. **claude-mem relicensed AGPL → Apache-2.0 at v13.0** (code now borrowable).
- **Current versions:** gbrain **v0.42** (was v0.22.4 — huge), MemPalace v3.5.0, claude-mem v13.8.0, cognee v1.2.2, codebase-memory-mcp v0.8.1, GitNexus v1.5.3, graphify 0.9.2.
- **Top steal candidates** (detail in the survey doc): ⭐ **Reciprocal Rank Fusion** — *convergent* (gbrain + GitNexus both) — principled hybrid fusion to replace our ad-hoc blend [LOW]; **cross-encoder reranker** (gbrain `zerank`/Zep) to kill "fresh-but-wrong" top hits [MED]; **Leiden/Louvain community detection** (graphify, ~270 LOC) to de-flatten the entity graph [LOW]; **bitemporal write-time guard** (MemPalace) [SMALL]; **capability-probed binary resolver + "parseable-response = only success"** (claude-mem) [SMALL]; **PreToolUse grep-intercept → inject memory** (codebase-memory-mcp) [LOW-MED].
- **Positioning sharpened:** the "vector store" strawman is dead — mem0/Letta/Zep/cognee all do hybrid/temporal/graph now (and now *lead* on graph reasoning). MM's durable wedge is **governance** (lifecycle/steward/citations/conflict) — none of them have it; mem0 went the opposite way (ADD-only, no conflict resolution). Reposition: **"we govern what's remembered" — curation over accumulation.**
- **codebase-memory-mcp** (~20.9k★): **assessed** — CODE-structure memory (tree-sitter graph; 99.2% token cut via a PreToolUse grep-intercept hook + `get_architecture` overview). Complementary to MM's life/claims memory, not a competitor.
- **My-Brain-Is-Full-Crew** (gnekt): the most **vision-aligned** peer to the user's **Atlas/Jarvis** layer (8 agents + 14 skills over Obsidian, dispatcher→delegate router, `weekly-agenda`/`email-triage` skills). Steal for Atlas orchestration, not MM's memory engine.

**Next re-survey target: ~2026-09** (or sooner if a memory project goes viral). Regenerate `artifacts/steal-from-others-<date>.md` + refresh this section.
