# MemoryMaster Improvement Plan

Generated: 2026-03-08 from deep audit + beads competitive analysis

## Audit Summary

| Feature Claimed | Reality | Verdict |
|---|---|---|
| 6-state lifecycle | Works correctly | REAL |
| Citation tracking | FK enforced, works | REAL |
| Auto-redaction | Works but missing JWT, SSH, OAuth patterns | PARTIAL |
| 12 MCP tools | All functional | REAL |
| Hybrid retrieval | Vector is a stub — only lexical+confidence+freshness | FAKE |
| Steward governance | semantic_probe NOT IMPLEMENTED — crashes if enabled | BROKEN |
| Tool probe | Works but arbitrary shell execution — security hole | UNSAFE |
| Dashboard | Dark theme, 2-col layout, working | REAL |
| LLM steward | Provider-agnostic (Gemini/OpenAI/Anthropic/Ollama), working | REAL |
| Incident drills / HMAC | Claimed but minimal, HMAC not in code | FAKE |
| Multi-tenancy | Scope is just a string, no enforcement | MISSING |

Production readiness: ~65%

---

## P0 — Critical Bugs (Must fix before anyone uses this)

### 1. Fix semantic_probe crash
- **File**: `memorymaster/steward.py`
- **Bug**: `run_semantic_probe()` called by default but function body missing
- **Impact**: Steward crashes with NameError if semantic probe enabled
- **Fix**: Implement semantic probe using embeddings, or disable by default and add stub

### 2. Sandbox tool_probe
- **File**: `memorymaster/steward.py`
- **Bug**: `subprocess.run()` on user-provided tool command with no sandboxing
- **Impact**: Arbitrary code execution (RCE vulnerability)
- **Fix**: Allowlist of safe commands, argument quoting, optional disable flag

### 3. Fix deterministic validators
- **File**: `memorymaster/jobs/deterministic.py`
- **Bug**: IP regex accepts `999.999.999.999` as valid
- **Impact**: False positive validations
- **Fix**: Add range checks (0-255 per octet), validate URL domains, calendar-valid dates

---

## P1 — Core Missing Features

### 4. Real vector search (sentence-transformers or Gemini embeddings)
- **File**: `memorymaster/embeddings.py` (stub exists), `memorymaster/retrieval.py`
- **Current**: vector_hook is empty callback placeholder, hash-based fallback
- **Fix**: Integrate `sentence-transformers` (MiniLM-L6-v2, 384-dim) or Gemini embedding API
- **Impact**: "Hybrid retrieval" claim becomes real. Semantic search actually works.

### 5. FTS5 for lexical search
- **File**: `memorymaster/storage.py`
- **Current**: LIKE-based substring search, O(n) table scan
- **Fix**: Create FTS5 virtual table mirroring claims text, use MATCH queries
- **Impact**: 10-100x faster text search, proper ranking with BM25

### 6. Memory decay / compaction summaries (inspired by beads)
- **File**: `memorymaster/jobs/compactor.py`
- **Current**: Compactor just archives old claims and trims events
- **Fix**: Use LLM to summarize groups of related archived claims into higher-level claims
- **Example**: 15 claims about "Cloudflare DNS debugging" → 1 summary claim with key findings
- **Impact**: Context window savings, knowledge distillation

### 7. Claim dependency graph (inspired by beads)
- **Schema**: New `claim_links` table: `(source_id, target_id, link_type, created_at)`
- **Link types**: `relates_to`, `supersedes`, `derived_from`, `contradicts`, `supports`
- **Current**: Claims are flat/disconnected, superseded is just a status
- **Impact**: Navigate knowledge as a graph, find related claims, trace provenance

### 8. Auto-validate after LLM extraction
- **Pipeline**: `llm_steward` → extract claims → auto-run `deterministic` + `filesystem_grep` probes
- **Current**: LLM steward and steward governance are completely disconnected
- **Fix**: Chain them: extract → validate → confirm/stale in one pass
- **Impact**: Higher quality confirmed claims with cross-validation

---

## P2 — Agent-Native Features

### 9. Context window optimizer
- **New method**: `query_for_context(token_budget=4000)`
- **Logic**: Score claims by relevance, pack into token budget using greedy knapsack
- **Output**: Formatted text block ready to inject into agent system prompt
- **Impact**: THE killer feature for AI agents — auto-curated memory that fits

### 10. Claim staleness detection via file watchers
- **Trigger**: When a source file cited by a claim changes (git diff, file watcher)
- **Action**: Auto-flag claim as `stale`, queue for re-validation
- **Current**: Staleness only detected during manual steward runs
- **Impact**: Memory stays fresh automatically

### 11. Deduplication engine
- **Method**: Embedding similarity (cosine > 0.92) + subject/predicate overlap
- **Action**: Merge duplicates — keep highest confidence, archive the rest
- **Current**: We went 160 → 853 claims, many are semantically identical
- **Impact**: Cleaner, more reliable knowledge base

### 12. Conflict auto-resolution
- **Logic**: When two claims conflict, auto-pick winner by:
  1. Higher confidence score
  2. More recent (fresher)
  3. More citations
  4. LLM tiebreaker (optional)
- **Current**: Conflicts require human review (impossible at scale)
- **Impact**: Zero-maintenance conflict handling

### 13. Agent-optimized JSON output
- **All CLI commands**: Add `--json` flag for structured output
- **Schema**: `{"ok": true, "data": [...], "meta": {"total": N, "query_ms": M}}`
- **Current**: Human-readable text output, hard to parse programmatically
- **Impact**: Agents can consume output directly (like beads)

### 14. Multi-key rotation for LLM steward
- **File**: `memorymaster/llm_steward.py`
- **Config**: `--api-keys key1,key2,key3` — rotate on 429
- **Current**: Single key, rate limited quickly on free tier
- **Impact**: 5x throughput on free Gemini tier

---

## P3 — Infrastructure (Production hardening)

### 15. Atomic operator queue
- **Current**: JSON file persistence, not atomic, crash = duplicate processing
- **Fix**: Use SQLite WAL table for pending turns instead of JSON file
- **Impact**: No data loss on crash

### 16. Multi-tenancy
- **Current**: `scope` is just a string column, no enforcement
- **Fix**: Add `tenant_id` column, enforce at storage layer, row-level isolation
- **Impact**: Safe for multi-agent/multi-project deployments

### 17. Configurable weights
- **Hardcoded**: Retrieval ranking (45/30/15/10), freshness half-life (168/72/24h), validation threshold (0.58)
- **Fix**: Config file or env vars for all tunable parameters
- **Impact**: Users can tune for their use case

### 18. More redaction patterns
- **Missing**: JWT (`eyJ...`), GitHub tokens (`ghp_`, `gho_`, `github_pat_`), SSH keys (OpenSSH format), OAuth bearer, env var format (`$API_KEY`)
- **Fix**: Add patterns to `security.py`
- **Impact**: Better secret protection

### 19. Postgres parity
- **Current**: 1 test skipped, feature incomplete
- **Fix**: Full test suite for postgres_store, migration scripts
- **Impact**: Production-grade for teams

### 20. Connection retry logic
- **Current**: No retries on DB connection failure
- **Fix**: Exponential backoff with 3 retries
- **Impact**: Resilience in flaky environments

---

## P4 — Competitive Features (from beads)

### 21. Hierarchical claim IDs
- **Format**: `mm-a3f8.1.1` — parent.child.grandchild
- **Benefit**: Group related claims under topics
- **beads equivalent**: `bd-a3f8.1.1` for epics/tasks/subtasks

### 22. "Ready" detection
- **Method**: `memorymaster ready` — show claims needing attention
- **Criteria**: Stale + high confidence (needs re-validation), conflicted (needs resolution), low-confidence candidates (needs more evidence)
- **beads equivalent**: `bd ready` — unblocked tasks

### 23. Per-claim audit trail
- **Method**: `memorymaster history <claim_id>` — full timeline of one claim
- **Output**: All transitions, validations, probes, confidence changes
- **beads equivalent**: `bd show <id>` with audit trail

### 24. Stealth mode
- **Method**: `memorymaster init --stealth` — local-only claims
- **Benefit**: Experiment without polluting shared memory
- **beads equivalent**: `bd init --stealth`

### 25. Git-backed versioning
- **Method**: Snapshot claim DB at each git commit
- **Benefit**: Rollback memory to any point in time
- **beads equivalent**: Dolt (SQL + git)

---

## Execution Phases

| Phase | Features | Effort | Impact |
|---|---|---|---|
| **Phase 1** | P0 bugs (#1-3) + FTS5 (#5) + dedup (#11) + multi-key (#14) | 1 day | Stops being broken, becomes searchable |
| **Phase 2** | Vector search (#4) + context optimizer (#9) + claim graph (#7) + JSON output (#13) | 2 days | Becomes genuinely useful for agents |
| **Phase 3** | Auto-staleness (#10) + conflict resolution (#12) + compaction summaries (#6) + auto-validate (#8) | 2 days | Becomes autonomous |
| **Phase 4** | Ready detection (#22) + audit trail (#23) + hierarchical IDs (#21) + stealth (#24) | 1 day | Competitive with beads |
| **Phase 5** | Multi-tenancy (#16) + postgres (#19) + git versioning (#25) + atomic queue (#15) | 2 days | Production-grade |

---

## Competitive Positioning

**beads**: Graph issue tracker for task management. Strong at: dependency tracking, multi-agent coordination, git-native.

**memorymaster**: Knowledge reliability system for persistent memory. Strong at: claim lifecycle, confidence scoring, citation tracking, LLM curation, conflict detection.

**They're complementary, not competitors.** beads tracks TASKS, memorymaster tracks KNOWLEDGE. An agent uses beads to know what to DO and memorymaster to know what it KNOWS.

Integration opportunity: memorymaster claims can reference beads task IDs. When a beads task completes, memorymaster auto-ingests the learnings.
