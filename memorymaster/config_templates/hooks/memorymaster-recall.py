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

try:
    data = json.loads(sys.stdin.read() or "{}")
    query = data.get("prompt", "")
    session_id = data.get("session_id", "")[:16]
    if len(query.split()) < 3:
        log_hook("recall", "skip", session=session_id, reason="short-query", words=len(query.split()))
        sys.exit(0)

    log_hook("recall", "start", session=session_id, query_len=len(query))
    from memorymaster.recall.context_hook import recall
    ctx = recall(query, db_path=DB_PATH, skip_qdrant=True)
    log_hook("recall", "done", session=session_id, hit=bool(ctx), ctx_chars=len(ctx or ""))
    if ctx:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "[MemoryMaster recall]\n" + ctx
            }
        }))
except Exception as e:
    log_hook("recall", "error", message=str(e)[:200])
