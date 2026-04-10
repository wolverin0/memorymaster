"""UserPromptSubmit hook: inject relevant MemoryMaster claims into Claude's context."""
import json, os, sys

PROJECT_ROOT = "__MEMORYMASTER_PROJECT_ROOT__"
DB_PATH = os.path.join(PROJECT_ROOT, "memorymaster.db")

sys.path.insert(0, PROJECT_ROOT)
os.environ["MEMORYMASTER_DEFAULT_DB"] = DB_PATH
os.chdir(PROJECT_ROOT)

try:
    data = json.loads(sys.stdin.read() or "{}")
    query = data.get("prompt", "")
    if len(query.split()) < 3:
        sys.exit(0)

    from memorymaster.context_hook import recall
    ctx = recall(query, db_path=DB_PATH, skip_qdrant=True)
    if ctx:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "[MemoryMaster recall]\n" + ctx
            }
        }))
except Exception:
    pass
