# What to steal from neighboring memory tools (2026-04-27)

Survey of 6 active memory/code-graph projects. Each entry: the feature, its source, why we want it, our equivalent (or gap), and effort estimate.

## TIER S — high-leverage, directly attacks our v3.6.0 null result

### 1. gbrain v0.21.0 "Code Cathedral II" — call-graph edges + two-pass retrieval + parent-scope chunking

**What it is:** structural retrieval upgrade. Instead of treating chunks as flat lexical units, gbrain emits per-symbol chunks with `parentSymbolPath` (e.g. `class Foo { m1(), m2() }` → 3 chunks: class header + each method tagged `['Foo']`). Then `--walk-depth 1..2` does **two-pass retrieval**: first lexical match, then graph-walk neighbors with `1/(1+hop)` decay (same formula as ours). New tables: `code_edges_chunk` + `code_edges_symbol` (resolved + unresolved), `qualified_name` indexes.

**Why we want it:** our v3.6.0 confirmed the GRAPH stream is FLAT. They use the same `1/(1+hop)` formula but get value from it because their graph is **structural** (call-edges, parent scope) not just **entity-fanout** (which is what we have). Their chunks retain semantic structure; ours don't.

**Our gap:** we only have entity-level fanout (`claims_for_entities_with_distance`). No structural call-graph between claims, no parent-scope chunking, no two-pass.

**Steal:**
- **High value:** the **two-pass retrieval pattern** is portable to our recall hook even without structural edges. After lexical recall, do a 2nd pass over the same set asking "which entities co-mentioned with these claims?" via the entity_aliases table. May unblock the GRAPH stream we proved flat.
- **Medium value:** tag each ingested claim with a "parent_scope" (the wiki article or canonical entity it talks about), so recall can boost claims whose parent matches the query.

**Effort:** 2-day research spike. Structural call-edges between claims = nontrivial schema change.

### 2. MemPalace v3.3.0 "Closets" — searchable index pointing to drawers

**What it is:** two-tier search. Closets are compact AAAK pointers (which drawer to open); search hits closets first (fast), then hydrates verbatim from drawers. **R@1: 0.42 → 0.58 (+38%)** with regex closets, +63% with LLM closets.

**Why we want it:** mirror of our wiki articles + claims, but with measurable retrieval lift. Our wiki is a READ layer for HUMANS; their closets are a READ layer for the SEARCHER. Different optimization target.

**Our gap:** wiki absorbed claims compile prose for humans; we don't use the compiled pointer as a search-side boost. Closets are search-time, not display-time.

**Steal:** a **closet table** that mirrors wiki articles but stores BM25-friendly pointers (entity terms, key concept tokens). Search → closet → expand to claims behind them. Direct claims search stays as the floor (closets only boost, never gate).

**Effort:** 1 week. New table + new ranking layer in recall + auto-populate on wiki-absorb.

### 3. MemPalace v3.3.0 "Halls" — content type routing

**What it is:** drawers tagged by content type (technical, emotions, family, memory, creative, identity, consciousness). Halls connect rooms within a wing by KIND of content. Search "emotional moments in my project" returns those.

**Why we want it:** our claim_type field exists (decision, gotcha, constraint, etc.) but **isn't used in recall ranking**. Only used for filter UI.

**Steal:** add claim_type-aware ranking to recall. If query has signal "decision" → boost decision-typed claims. Already partially present via classify hook (DECISION/GOTCHA signals); make it propagate to ranking weights.

**Effort:** 0.5 day. Smallest change with highest measurability.

## TIER A — improves operational quality

### 4. MemPalace v3.3.3 — entity detection overhaul

**What it is:** canonical project names from package manifests (package.json, pyproject.toml, Cargo.toml, go.mod), real people from git commit authors. Union-find dedup across name/email aliases. Bot filtering. CamelCase extraction so `MemPalace`/`ChromaDB` aren't fragmented. Tightened versioned/hyphenated patterns to kill `context-manager`/`multi-word` false positives.

**Why we want it:** our entity_extractor regex still misses canonical project names and over-extracts noise like `multi-word`. They've battle-tested the patterns.

**Steal:** port their CamelCase + tightened-hyphen regex into our `entity_extractor.py` Layer-1. Adapter for git authors as a new entity source.

**Effort:** 0.5 day for regex update. 1 day for git-authors integration.

### 5. MemPalace v3.3.3 — file/conversation scanner with `cwd` metadata

**What it is:** `~/.claude/projects/<slug>/` folders contribute project entities using each session's authoritative `cwd` metadata, avoiding slug-decoding ambiguity (which we have hit before with our hash-suffix scope bug).

**Why we want it:** we ALREADY suffered the slug-decoding bug (v3.3.1 fix). They solve it the right way: read `cwd` from transcript metadata.

**Steal:** in our auto-ingest hook, when deriving scope, prefer `cwd` from the JSONL session file over decoding the slug.

**Effort:** 1 hour. Pure improvement.

### 6. graphify v0.5.0 — `merge-graphs` + `clone <github-url>`

**What it is:** cross-repo knowledge graphs (every node carries `repo` tag, you can filter), one-command clone-and-analyze.

**Why we want it:** we have 35 graphify-out folders one per project. Cross-project intelligence is currently manual.

**Our gap:** no `federated_query` across project_X + project_Y entity registries.

**Steal:** wrap graphify's `merge-graphs` in a `federated-graphify` MCP tool; surface cross-project entities in our recall (when scope=global).

**Effort:** 1 day.

### 7. claude-mem v12.4 — multi-account isolation via `CLAUDE_MEM_DATA_DIR` + per-UID worker port

**What it is:** `37700 + uid % 100` formula avoids port collisions when multiple users run claude-mem on the same machine. `CLAUDE_MEM_INTERNAL=1` trust boundary.

**Why we want it:** if we ever ship a server/dashboard mode with multiple sessions, this pattern saves an incident.

**Steal (deferred):** for `run-dashboard`, port = `37800 + uid % 100`. Trivial.

**Effort:** 1 hour, deferred until dashboard sees multi-user use.

## TIER B — interesting but lower ROI for us right now

### 8. gbrain v0.22.0 — source-aware search ranking

**What it is:** SQL-layer source-factor multiplier. Hard-exclude prefixes (`test/`, `archive/`, `attachments/`, `.raw/`) at chunk-rank stage. Curated pages always beat chat-log dumps.

**Why we want it (less):** we don't have "chat logs vs curated" distinction at the file level. Our equivalent would be "tier=core beats tier=peripheral", which we already encode via the `tier` column.

**Steal:** validate that our `tier` field is actually consulted in ranking. If yes, document. If no, wire it.

**Effort:** 0.5 day — investigation + possibly a wiring fix.

### 9. gbrain v0.22.4 — frontmatter-guard CLI + auto-fix

**What it is:** `gbrain frontmatter validate <path> [--fix]` + `frontmatter audit` + `frontmatter install-hook`. Seven canonical validation codes. Backup .bak before write. Pre-commit hook integration.

**Why we want it:** we have `validate-wiki` hook but no auto-fix CLI. When we hand-write wiki articles and forget a tag, only get a hook warning, not an automatic fix.

**Steal:** new CLI `memorymaster wiki-validate <path> [--fix] [--audit]` that auto-fixes the 4 fixable codes.

**Effort:** 1 day.

### 10. claude-mem v12.4 — "cynical deletion" pattern (defenders + tolerators)

**What it is:** philosophy. Closed 27 issues by removing two anti-patterns: defenders (orphan cleanup, duplicate liveness probes) and tolerators (silent JSON drops, drifted SSE filters, passthrough Zod schemas). Replaced with strict boundaries.

**Why we want it:** good audit lens for our own code. We have many defensive `try: ... except: pass` blocks.

**Steal:** schedule a "defender + tolerator audit" sprint. Identify silent failures and replace with strict errors at boundaries.

**Effort:** 1 week if done thoroughly.

### 11. My-Brain-Is-Full-Crew — multi-platform adapter (Claude Code / Gemini CLI / OpenCode / Codex)

**What it is:** single source of truth, builds for 4 platforms. Source files use platform-neutral vocabulary (`capabilities:` instead of `tools:`, `.platform/` placeholder).

**Why we want it (less):** our hooks are Claude Code + Codex bilateral already. Gemini CLI / OpenCode users are not our current audience.

**Steal:** if we ever sell to users on those platforms, this is the reference. Skip for now.

### 12. GitNexus v1.6.4 RC — Cross-repo impact analysis (`@repo` MCP routing)

**What it is:** impact queries can span multiple indexed repositories.

**Why we want it (less):** we don't index code with GitNexus from MemoryMaster's POV — we index claims. Cross-repo claim impact would be a different feature.

**Steal:** confirm our `federated_query` handles multi-DB queries cleanly. Skip otherwise.

## Recommended ship plan

**v3.7.0 (1 week):**
- TIER S #3: claim_type-aware ranking (0.5 day)
- TIER A #4: MemPalace entity detection regex port (1 day)
- TIER A #5: cwd-from-transcript scope derivation (1 hour)
- Bonus: TIER B #9: wiki-validate auto-fix CLI (1 day)

**v3.8.0 (2 weeks):**
- TIER S #1 (research spike): two-pass retrieval w/ entity-fanout, see if it lifts the GRAPH stream
- TIER S #2: closets/wiki-pointer search-side boost
- TIER A #6: federated-graphify

**v3.9.0+ (deferred):**
- TIER S #1 full: structural edges between claims (parent-scope chunking)
- TIER B #10: cynical-deletion audit sweep

## Honest non-goals

- Multi-platform adapter (My-Brain). We're Claude+Codex first-class; not Gemini CLI / OpenCode market.
- claude-mem Docker / SWE-bench harness. Different distribution model from us.
- GitNexus cross-repo `@repo` routing — we don't have the symbol-level use case.
