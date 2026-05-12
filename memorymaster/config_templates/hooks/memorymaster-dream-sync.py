"""Stop hook: sync MemoryMaster claims with Claude Code Auto Dream on session end."""
import os
import sys

PROJECT_ROOT = "__MEMORYMASTER_PROJECT_ROOT__"
DB_PATH = os.path.join(PROJECT_ROOT, "memorymaster.db")

sys.path.insert(0, PROJECT_ROOT)
os.environ["MEMORYMASTER_DEFAULT_DB"] = DB_PATH
os.chdir(PROJECT_ROOT)

from memorymaster.hook_log import log_hook

log_hook("dream-sync", "start")
try:
    sys.stdin.read()

    from memorymaster.dream_bridge import dream_sync
    result = dream_sync(DB_PATH, project_path=PROJECT_ROOT, min_quality=0.5, max_memories=30)
    seeded = result.get("seeded", 0)
    ingested = result.get("ingested", 0)

    log_hook("dream-sync", "done", seeded=seeded, ingested=ingested)
    if seeded or ingested:
        sys.stderr.write(f"[MemoryMaster] dream-sync: seeded={seeded} ingested={ingested}\n")
except Exception as e:
    log_hook("dream-sync", "error", message=str(e)[:200])
    sys.stderr.write(f"[MemoryMaster] dream-sync error: {e}\n")
