"""Stop hook: sync MemoryMaster claims with Claude Code Auto Dream on session end."""
import os
import sys

PROJECT_ROOT = "__MEMORYMASTER_PROJECT_ROOT__"
DB_PATH = os.path.join(PROJECT_ROOT, "memorymaster.db")

sys.path.insert(0, PROJECT_ROOT)
os.environ["MEMORYMASTER_DEFAULT_DB"] = DB_PATH
os.chdir(PROJECT_ROOT)

# Must import after the sys.path.insert bootstrap above — hook templates run
# standalone, before the package is necessarily on PYTHONPATH.
from memorymaster.hook_log import log_hook  # noqa: E402

log_hook("dream-sync", "start")
try:
    sys.stdin.read()

    from memorymaster.dream_bridge import dream_sync
    # Under MEMORYMASTER_WAL_DISCIPLINE=1 the ingest half appends op:"dream"
    # spool envelopes instead of opening the DB (P1 spec §2.3) — dream_ingest
    # reads the flag itself, so this template only needs the inherited env var.
    result = dream_sync(DB_PATH, project_path=PROJECT_ROOT, min_quality=0.5, max_memories=30)
    # dream_sync returns nested {"ingest": {...}, "seed": {...}} stats; the
    # previous flat .get("seeded") always logged 0.
    ingest_stats = result.get("ingest", {}) or {}
    seed_stats = result.get("seed", {}) or {}
    seeded = seed_stats.get("seeded", 0)
    ingested = ingest_stats.get("ingested", 0)
    spooled = ingest_stats.get("spooled", 0)

    log_hook("dream-sync", "done", seeded=seeded, ingested=ingested, spooled=spooled)
    if seeded or ingested or spooled:
        sys.stderr.write(
            f"[MemoryMaster] dream-sync: seeded={seeded} ingested={ingested} spooled={spooled}\n"
        )
except Exception as e:
    log_hook("dream-sync", "error", message=str(e)[:200])
    sys.stderr.write(f"[MemoryMaster] dream-sync error: {e}\n")
