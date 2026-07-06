"""Scheduled task: run MemoryMaster steward cycle."""
import os
import sys

PROJECT_ROOT = "__MEMORYMASTER_PROJECT_ROOT__"
DB_PATH = os.path.join(PROJECT_ROOT, "memorymaster.db")

sys.path.insert(0, PROJECT_ROOT)
os.environ["MEMORYMASTER_DEFAULT_DB"] = DB_PATH

# LLM stack: claude_cli (Claude Code OAuth via local `claude --print`) is the
# primary, with Ollama gemma4:e4b as a defensive fallback. Direct assignment
# (NOT setdefault) — the hook MUST own these vars so an inherited shell env
# can't silently route LLM calls to a stale provider. Bug observed 2026-04-25:
# setdefault was a no-op when the inherited env already had MEMORYMASTER_LLM_PROVIDER
# set, so the new model name routed to the OLD provider → 50× HTTP 404 per cycle
# before the fallback chain saved it. Captured as v3.5.0 release notes.
os.environ["MEMORYMASTER_LLM_PROVIDER"] = "claude_cli"
os.environ["MEMORYMASTER_LLM_MODEL"] = "claude-haiku-4-5-20251001"
os.environ["MEMORYMASTER_LLM_FALLBACK_PROVIDER"] = "ollama"
os.environ["MEMORYMASTER_LLM_FALLBACK_MODEL"] = "gemma4:e4b"

# v3.13 pre-steward Jaccard dedupe — shadow mode (count would-archive but
# never act). Direct assignment so an inherited shell env can't accidentally
# flip _SHADOW=0 and start archiving without operator review.
os.environ["MEMORYMASTER_DEDUPE_ENABLED"] = "1"
os.environ["MEMORYMASTER_DEDUPE_SHADOW"] = "1"
os.environ["MEMORYMASTER_DEDUPE_JACCARD_HIGH"] = "0.85"

os.chdir(PROJECT_ROOT)

try:
    from memorymaster.core.service import MemoryService
    from pathlib import Path

    svc = MemoryService(db_target=DB_PATH, workspace_root=Path(PROJECT_ROOT))
    # batch_limit threads into the validator/extractor/deterministic/decay jobs
    # (each defaults to 200). That path is deterministic (~0 LLM calls, ~12s per
    # 200, ~107s for 2000), so a large batch is cheap and keeps the candidate
    # backlog from outgrowing the steward when many panes ingest concurrently.
    result = svc.run_cycle(batch_limit=2000)
    print(f"[MemoryMaster] steward cycle: {result}")
except Exception as e:
    print(f"[MemoryMaster] steward error: {e}", file=sys.stderr)

# Auto-archive: stale claims never accessed, older than 14 days
try:
    from datetime import datetime, timedelta

    # Uniform pragma envelope (WAL + busy_timeout=15000) — a raw connect here
    # had busy_timeout=0 and could lose the UPDATE to a write race (spec F8).
    from memorymaster.stores._storage_shared import open_conn
    cutoff = (datetime.now() - timedelta(days=14)).isoformat()
    conn = open_conn(DB_PATH)
    conn.execute("""
        UPDATE claims SET status = 'archived', archived_at = datetime('now')
        WHERE status = 'stale' AND access_count = 0 AND created_at < ?
    """, (cutoff,))
    archived = conn.total_changes
    conn.commit()
    conn.close()
    if archived:
        print(f"[MemoryMaster] auto-archived {archived} stale unused claims")
except Exception as e:
    print(f"[MemoryMaster] auto-archive error: {e}", file=sys.stderr)

# Reclaim isolated claude_cli scratch transcripts so they never grow unbounded.
# _call_claude_cli runs `claude --print` from a scratch cwd; those session
# transcripts pile up in a separate ~/.claude/projects/ folder — purge the old ones.
try:
    from memorymaster.core.llm_provider import purge_claude_cli_scratch
    _p = purge_claude_cli_scratch()
    if _p.get("removed"):
        print(f"[MemoryMaster] purged {_p['removed']} old claude_cli scratch transcripts")
except Exception as e:
    print(f"[MemoryMaster] scratch purge error: {e}", file=sys.stderr)

# Wiki layer (Obsidian markdown) — OPT-IN, default OFF (2026-07-06).
# The claims DB + FTS5 + Qdrant + entity graph + recall IS the scalable "LLM
# wiki"; the markdown vault is a redundant, non-scaling duplicate that grows
# unbounded (real install hit 2 GB / 5,921 files, hung Obsidian) and its
# absorb runs the claude_cli stack in a loop — a heavy source of headless
# session churn. Nothing in recall depends on it (the only reader, the Closets
# stream, is default-OFF). Set MEMORYMASTER_WIKI_ABSORB=1 to enable it.
if os.environ.get("MEMORYMASTER_WIKI_ABSORB", "0").strip().lower() in ("1", "true", "yes"):
    try:
        from memorymaster.knowledge.wiki_engine import absorb
        wiki_path = os.path.join(PROJECT_ROOT, "obsidian-vault", "wiki")
        stats = absorb(DB_PATH, wiki_path)
        print(f"[MemoryMaster] wiki absorb: {stats}")
    except Exception as e:
        print(f"[MemoryMaster] wiki absorb error: {e}", file=sys.stderr)
