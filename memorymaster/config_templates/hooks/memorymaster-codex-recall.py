"""Thin Codex wire adapter around the installed, shared Claude recall hooks.

The companion scripts are rendered from the same templates as Claude's hooks;
ranking, scope, sensitivity, projections and delivery policy stay there.
Internal extractors/workers must inherit MEMORYMASTER_INTERNAL_CODEX=1 (or
MEMORYMASTER_INTERNAL_RUN=1) so their own Codex sessions never recall recursively.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import runpy
import sys
from contextlib import redirect_stderr, redirect_stdout

PROJECT_ROOT = "__MEMORYMASTER_PROJECT_ROOT__"
# Even adversarial Unicode stays below ~2,500 tokens: at most 1,800 UTF-8 bytes.
MAX_CONTEXT_BYTES = 1800
HOOKS = {
    "SessionStart": "memorymaster-session-start.py",
    "UserPromptSubmit": "memorymaster-recall.py",
}


def main(event: str) -> int:
    try:
        if event not in HOOKS or any(
            os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}
            for name in ("MEMORYMASTER_INTERNAL_CODEX", "MEMORYMASTER_INTERNAL_RUN")
        ):
            return 0
        data = json.loads(sys.stdin.read())
        if not isinstance(data, dict):
            return 0
        if any(key in data and not isinstance(data[key], str) for key in ("cwd", "prompt", "session_id")):
            return 0
        # SessionStart's shared scope resolver uses process cwd; prompt recall
        # uses hook_data.cwd. Normalize both to the actual Codex workspace.
        if data.get("cwd"):
            os.chdir(data["cwd"])
        captured = io.StringIO()
        original_stdin = sys.stdin
        try:
            sys.stdin = io.StringIO(json.dumps(data))
            with redirect_stdout(captured), redirect_stderr(io.StringIO()):
                try:
                    # The shared hook's stdout is captured here, not delivered.
                    # Only this adapter records the final, capped Codex output.
                    runpy.run_path(
                        str(Path(__file__).with_name(HOOKS[event])), run_name="__main__",
                        init_globals={"MEMORYMASTER_DEFER_HOOK_EVENT": True},
                    )
                except SystemExit as exc:
                    if exc.code not in (None, 0):
                        return 0
        finally:
            sys.stdin = original_stdin
        result = json.loads(captured.getvalue())
        context = result["hookSpecificOutput"]["additionalContext"]
        if not isinstance(context, str) or not context.strip():
            return 0
        context = context.encode("utf-8", errors="replace")[:MAX_CONTEXT_BYTES].decode("utf-8", errors="ignore")
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": event, "additionalContext": context,
        }}))
        sys.stdout.flush()
        try:
            sys.path.insert(0, PROJECT_ROOT)
            from memorymaster.surfaces.hook_events import append_hook_event

            append_hook_event("codex", event, 1)
        except Exception:
            pass
    except (Exception, SystemExit):
        pass
    return 0


if __name__ == "__main__":
    event = sys.argv[2] if len(sys.argv) == 3 and sys.argv[1] == "--event" else ""
    raise SystemExit(main(event))
