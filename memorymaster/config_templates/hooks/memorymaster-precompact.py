"""PreCompact hook: ALWAYS block before compaction, force save everything.

When Claude Code is about to compress context to free up the context window,
this hook blocks and forces Claude to save ALL important context to MemoryMaster
before it's lost. This is the safety net — compaction = permanent context loss.
"""
import json
import os
import sys
from datetime import datetime

STATE_DIR = os.path.join(os.path.expanduser("~"), ".memorymaster", "hook_state")
os.makedirs(STATE_DIR, exist_ok=True)


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}

    session_id = data.get("session_id", "unknown")

    with open(os.path.join(STATE_DIR, "hook.log"), "a") as f:
        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] PRE-COMPACT triggered for {session_id}\n")

    # ALWAYS block — compaction means permanent context loss
    sys.stdout.write(json.dumps({
        "decision": "block",
        "reason": "COMPACTION IMMINENT — context will be permanently lost. Before compaction, save ALL important context to MemoryMaster using mcp__memorymaster__ingest_claim. Save: every decision made, every bug root cause, every architecture choice, every gotcha discovered, every constraint learned. Set source_agent to 'claude-session'. Be thorough — after compaction you will NOT remember any of this. NEVER ingest credentials, IPs, or tokens. After saving everything, allow compaction to proceed."
    }))


if __name__ == "__main__":
    main()
