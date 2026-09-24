# MEMORYMASTER_MANAGED_RECALL_V1
# UserPromptSubmit recall hook. P1 WAL-discipline (spec §2.2): when the
# inherited environment carries MEMORYMASTER_WAL_DISCIPLINE=1, recall()
# below opens the DB strictly read-only (mode=ro + query_only — this
# per-prompt process can never take a write lock on the shared multi-GB
# file) and spools its access/feedback records to ~/.memorymaster/spool/
# for the steward drain. Flag unset/0 = the legacy read-write path.
import json
import os
import sys

PROJECT_ROOT = "__MEMORYMASTER_PROJECT_ROOT__"
DB_PATH = os.path.join(PROJECT_ROOT, "memorymaster.db")

sys.path.insert(0, PROJECT_ROOT)
os.environ["MEMORYMASTER_DEFAULT_DB"] = DB_PATH
os.chdir(PROJECT_ROOT)

from memorymaster.core.hook_log import log_hook  # noqa: E402 — import must follow sys.path bootstrap
from memorymaster.recall.delivery import deliver, is_automated  # noqa: E402

try:  # 4.9.0+: recall_block and recall(hook_data=...) ship together
    from memorymaster.recall.delivery import recall_block  # noqa: E402
    JEV_OPT_IN = True
except ImportError:  # a package older than this template: legacy prefix, no Jev opt-in
    def recall_block(context):
        return "[MemoryMaster recall]\n" + context
    JEV_OPT_IN = False

try:
    data = json.loads(sys.stdin.read() or "{}")
    query = data.get("prompt", "")
    query = query if isinstance(query, str) else ""
    session_id = str(data.get("session_id", ""))[:16]
    if is_automated(query):
        log_hook("recall", "skip", session=session_id, reason="automated-event")
    elif len(query.split()) < 3:
        log_hook("recall", "skip", session=session_id, reason="short-query", words=len(query.split()))
    else:
        log_hook("recall", "start", session=session_id, query_len=len(query))
        from memorymaster.recall.context_hook import recall
        # hook_data opts into the S2 RECALL Jev surface (inert unless MEMORYMASTER_JEV_* is on).
        extra = {"hook_data": data} if JEV_OPT_IN else {}
        ctx = recall(query, db_path=DB_PATH, skip_qdrant=True, **extra)
        if ctx:
            def emit(output):
                sys.stdout.write(output)
                sys.stdout.flush()
            sent = deliver(data, recall_block(ctx), emit)
            log_hook("recall", "done", session=session_id, hit=True, delivered=sent)
except Exception as e:
    log_hook("recall", "error", message=str(e)[:200])
