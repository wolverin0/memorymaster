import json, os, sys

PROJECT_ROOT = "__MEMORYMASTER_PROJECT_ROOT__"
DB_PATH = os.path.join(PROJECT_ROOT, "memorymaster.db")

sys.path.insert(0, PROJECT_ROOT)
os.environ["MEMORYMASTER_DEFAULT_DB"] = DB_PATH
os.chdir(PROJECT_ROOT)

from memorymaster.hook_log import log_hook

try:
    data = json.loads(sys.stdin.read() or "{}")
    query = data.get("prompt", "")
    session_id = data.get("session_id", "")[:16]
    if len(query.split()) < 3:
        log_hook("recall", "skip", session=session_id, reason="short-query", words=len(query.split()))
        sys.exit(0)

    log_hook("recall", "start", session=session_id, query_len=len(query))
    from memorymaster.context_hook import recall
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
