# PreToolUse recall hook (opt-in). Intercepts Grep/Glob and injects
# relevant memory as additionalContext, so an agent about to search the
# repo also gets what MemoryMaster already knows about the query. Pattern
# borrowed from codebase-memory-mcp (source key: pretooluse_grep_inject).
#
# DEFAULT-OFF: this stacks a SECOND recall injection on top of the
# UserPromptSubmit recall hook, so it stays dark unless the operator opts
# in with MEMORYMASTER_PRETOOLUSE_RECALL=1. Flag unset/0 => emit nothing,
# exit 0 (pure passthrough — the tool call proceeds unchanged).
import json
import os
import sys

PROJECT_ROOT = r"__MEMORYMASTER_PROJECT_ROOT__"
DB_PATH = os.path.join(PROJECT_ROOT, "memorymaster.db")

sys.path.insert(0, PROJECT_ROOT)
os.environ["MEMORYMASTER_DEFAULT_DB"] = DB_PATH
os.chdir(PROJECT_ROOT)

from memorymaster.core.hook_log import log_hook  # noqa: E402 — import must follow sys.path bootstrap


def _is_enabled() -> bool:
    return os.environ.get("MEMORYMASTER_PRETOOLUSE_RECALL", "").strip().lower() in ("1", "true", "yes", "on")


try:
    if not _is_enabled():
        sys.exit(0)  # opt-in flag not set: pure passthrough, inject nothing

    data = json.loads(sys.stdin.read() or "{}")
    tool_name = data.get("tool_name", "")
    session_id = data.get("session_id", "")[:16]
    tool_input = data.get("tool_input", {}) or {}
    # Grep uses `pattern`, Glob uses `pattern` too; fall back to `query`.
    query = (tool_input.get("pattern") or tool_input.get("query") or "").strip()

    if tool_name not in ("Grep", "Glob") or len(query.split()) < 2:
        log_hook("pretooluse-recall", "skip", session=session_id, tool=tool_name, reason="not-eligible")
        sys.exit(0)

    log_hook("pretooluse-recall", "start", session=session_id, tool=tool_name, query_len=len(query))
    from memorymaster.recall.context_hook import recall
    ctx = recall(query, db_path=DB_PATH, skip_qdrant=True)
    log_hook("pretooluse-recall", "done", session=session_id, hit=bool(ctx), ctx_chars=len(ctx or ""))
    if ctx:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": "[MemoryMaster recall]\n" + ctx
            }
        }))
except Exception as e:
    log_hook("pretooluse-recall", "error", message=str(e)[:200])
