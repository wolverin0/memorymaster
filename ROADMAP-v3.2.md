# MemoryMaster v3.2 Roadmap — MemPalace-Inspired Upgrades

Inspired by [MemPalace](https://github.com/milla-jovovich/mempalace) (96.6% LongMemEval).

## Priority 1: Hooks (capture everything)

### 1.1 Block-based Stop hook
Replace passive Gemini extraction with MemPalace's approach: count human messages in transcript, every N messages BLOCK Claude from stopping and force a save. Uses `decision: block` + reason as system message.

**Files:** `~/.claude/hooks/memorymaster-auto-ingest.py`
**Behavior:** Every 15 human messages → block → Claude saves to MemoryMaster → next stop allowed

### 1.2 PreCompact hook
New hook: fires before context compaction. ALWAYS blocks, forces Claude to save everything before context is lost. Critical safety net.

**Files:** `~/.claude/hooks/memorymaster-precompact.py`, update `~/.claude/settings.json`

## Priority 2: Temporal validity on claims

### 2.1 Add valid_from / valid_until to claims
Claims already have these columns but they're never used. When ingesting, set `valid_from` to now. When a claim is superseded, set `valid_until` on the old one. Query with `as_of` date parameter to get point-in-time truth.

**Files:** `memorymaster/service.py`, `memorymaster/mcp_server.py`, `memorymaster/storage.py`

## Priority 3: Duplicate check on ingest

### 3.1 Content hash dedup
Before inserting a new claim, hash the normalized text and check against existing claims. Skip if duplicate. Faster than waiting for the steward to dedup.

**Files:** `memorymaster/service.py`, `memorymaster/mcp_server.py`

## Priority 4: Conversation mining

### 4.1 Ingest transcripts as claims
Parse Claude Code JSONL transcripts and ingest assistant messages as claims. Similar to MemPalace's `convo_miner.py` but for our DB.

**Files:** `memorymaster/transcript_miner.py` (new), CLI command `memorymaster mine-transcript`

## Priority 5: LongMemEval benchmark

### 5.1 Benchmark runner
Script that loads LongMemEval dataset, ingests sessions as claims, queries MemoryMaster, and outputs in evaluation format.

**Files:** `benchmarks/longmemeval_runner.py` (new)

## Not implementing (from MemPalace)

- **AAAK compression** — experimental, scores 12% worse than raw mode
- **Palace architecture (wings/halls/rooms)** — our scope/wiki structure covers this differently
- **ChromaDB** — we use SQLite FTS5 + Qdrant, no need to add another vector DB
- **Local entity detector** — nice but our Gemini-based extraction is more accurate
