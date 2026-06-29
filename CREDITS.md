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

**New candidates to evaluate** (found post-survey, not yet assessed):

| Project | Stars | What it is | Why look |
|---|---|---|---|
| **DeusData/codebase-memory-mcp** | ~20.9k | MCP server building a SQLite **code** knowledge graph via tree-sitter ASTs + "Hybrid LSP" type resolution; 14 tools, 158 langs, single static C binary, 3D graph viz | Claims **99.2% token reduction** vs file-grep, **sub-ms** queries (indexed 28M-LOC Linux kernel in 3 min). Directly relevant to our GRAPH/retrieval stream (the v3.9 TIER-S #1 we proved flat). The C-binary zero-dep packaging + RAM-first indexing are notable. **Code memory, not life/claims memory** — complementary, not a competitor. |
| _(add as found)_ | | | |

**Next re-survey target: ~2026-09** (or sooner if a memory project goes viral). When doing it,
regenerate `artifacts/steal-from-others-<date>.md` and fold the deltas back into this table.
