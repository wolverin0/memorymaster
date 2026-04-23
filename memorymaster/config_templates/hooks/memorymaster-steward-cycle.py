"""Scheduled task: run MemoryMaster steward cycle."""
import os, sys

PROJECT_ROOT = "__MEMORYMASTER_PROJECT_ROOT__"
DB_PATH = os.path.join(PROJECT_ROOT, "memorymaster.db")

sys.path.insert(0, PROJECT_ROOT)
os.environ["MEMORYMASTER_DEFAULT_DB"] = DB_PATH
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

# Wiki absorb (compiled truth + timeline articles)
try:
    # Keys come from the rotator file (~/.memorymaster/gemini-keys.env) or a
    # singular GEMINI_API_KEY env var. Hook must never hardcode credentials.
    os.environ.setdefault("MEMORYMASTER_LLM_PROVIDER", "google")
    from memorymaster.wiki_engine import absorb
    wiki_path = os.path.join(PROJECT_ROOT, "obsidian-vault", "wiki")
    stats = absorb(DB_PATH, wiki_path)
    print(f"[MemoryMaster] wiki absorb: {stats}")
except Exception as e:
    print(f"[MemoryMaster] wiki absorb error: {e}", file=sys.stderr)
