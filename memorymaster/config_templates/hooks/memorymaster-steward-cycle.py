"""Scheduled task: run MemoryMaster steward cycle."""
import os, sys

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
    from memorymaster.service import MemoryService
    from pathlib import Path

    svc = MemoryService(db_target=DB_PATH, workspace_root=Path(PROJECT_ROOT))
    result = svc.run_cycle()
    print(f"[MemoryMaster] steward cycle: {result}")
except Exception as e:
    print(f"[MemoryMaster] steward error: {e}", file=sys.stderr)

# Auto-archive: stale claims never accessed, older than 14 days
try:
    import sqlite3
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=14)).isoformat()
    conn = sqlite3.connect(DB_PATH)
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

# Wiki absorb (compiled truth + timeline articles). Inherits the LLM provider
# block above — uses the same OAuth-backed haiku stack as the steward.
try:
    from memorymaster.wiki_engine import absorb
    wiki_path = os.path.join(PROJECT_ROOT, "obsidian-vault", "wiki")
    stats = absorb(DB_PATH, wiki_path)
    print(f"[MemoryMaster] wiki absorb: {stats}")
except Exception as e:
    print(f"[MemoryMaster] wiki absorb error: {e}", file=sys.stderr)
