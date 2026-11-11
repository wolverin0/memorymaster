"""Thin Codex wire adapter around the installed, shared Claude recall hooks.

The companion scripts are rendered from the same templates as Claude's hooks;
ranking, scope, sensitivity, projections and delivery policy stay there.
Internal extractors/workers must inherit MEMORYMASTER_INTERNAL_CODEX=1 (or
MEMORYMASTER_INTERNAL_RUN=1) so their own Codex sessions never recall recursively
(MEMORYMASTER_RECALL_MACHINE_FILTER_CODEX=0 disables that check and the notice filter).
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
TRUNCATED_LINE = "[... truncated]"
HOOKS = {
    "SessionStart": "memorymaster-session-start.py",
    "UserPromptSubmit": "memorymaster-recall.py",
}


def fit_context(context: str) -> str:
    """Cut at the last whole line inside MAX_CONTEXT_BYTES and say so on a final line within the same budget.

    A first non-blank line that alone exceeds the budget keeps the byte-safe cut (never mid-UTF-8 sequence).
    """
    raw = context.encode("utf-8", errors="replace")
    if len(raw) <= MAX_CONTEXT_BYTES:
        return raw.decode("utf-8", errors="ignore")
    budget = MAX_CONTEXT_BYTES - len(("\n" + TRUNCATED_LINE).encode("utf-8"))
    kept, used = [], 0
    for line in raw.decode("utf-8", errors="ignore").splitlines(keepends=True):
        size = len(line.encode("utf-8"))
        if used + size > budget:
            break
        kept.append(line)
        used += size
    head = "".join(kept).rstrip("\r\n")
    if not head.strip():
        return raw[:MAX_CONTEXT_BYTES].decode("utf-8", errors="ignore")
    return head + "\n" + TRUNCATED_LINE


def main(event: str) -> int:
    try:
        if event not in HOOKS:
            return 0
        # Script execution only puts the hooks directory on sys.path. Retain
        # the launch root before the shared hooks change cwd, so source-tree
        # launches can still import the shared receipt API. Prefer the rendered
        # code root for installed hooks, including separate data-root installs.
        sys.path.insert(0, str(Path.cwd()))
        sys.path.insert(0, PROJECT_ROOT)
        try:
            from memorymaster.recall.machine_traffic import filter_enabled, internal_run
        except ImportError:  # a package older than this template: the inline pre-T-2273 check, no rollback flag
            def internal_run():
                return any(os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}
                           for name in ("MEMORYMASTER_INTERNAL_CODEX", "MEMORYMASTER_INTERNAL_RUN"))

            def filter_enabled(adapter):
                return True

        # The shared check, before stdin is even read (T-2273). MEMORYMASTER_RECALL_MACHINE_FILTER_CODEX=0
        # rolls it back too, so the Codex adapter recalls even for internal runs.
        if filter_enabled("codex") and internal_run():
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
        shared: dict = {}
        confirm = None
        try:
            from memorymaster.recall.delivery import confirm_turn as confirm
        except ImportError:  # a package older than this template: no delivery receipts
            pass
        try:
            sys.stdin = io.StringIO(json.dumps(data))
            with redirect_stdout(captured), redirect_stderr(io.StringIO()):
                try:
                    # The shared hook's stdout is captured here, not delivered.
                    # Only this adapter records the final, capped Codex output.
                    shared = runpy.run_path(
                        str(Path(__file__).with_name(HOOKS[event])), run_name="__main__",
                        init_globals={"MEMORYMASTER_DEFER_HOOK_EVENT": True,
                                      "MEMORYMASTER_RECALL_ADAPTER": "codex"},
                    )
                except SystemExit as exc:
                    if exc.code not in (None, 0):
                        return 0
        finally:
            sys.stdin = original_stdin
        # T-2275: the shared hook only staged the claim ids; they are "seen" and counted once THIS write
        # succeeded, and only the ids that survive the byte cut.
        turn_data = shared.get("data") if isinstance(shared.get("data"), dict) else data  # carries the turn id
        confirmable = confirm is not None and event == "UserPromptSubmit" and shared.get("via") in ("shared", "local")
        emitted, failure = "", shared.get("delivery_failure") or "not_emitted"
        try:
            result = json.loads(captured.getvalue())
            context = result["hookSpecificOutput"]["additionalContext"]
            if not isinstance(context, str) or not context.strip():
                return 0
            context = fit_context(context)
            failure = "write_failure"
            sys.stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": event, "additionalContext": context,
            }}))
            sys.stdout.flush()
            emitted = context
            if confirmable and shared.get("delivered_block"):
                # the shared hook left the repeat fingerprint to this write: a failed write retries at once
                from memorymaster.recall.delivery import record_delivered

                record_delivered(turn_data, shared["delivered_block"])
        finally:
            if confirmable:
                confirm(turn_data, "codex", emitted=emitted, query=data.get("prompt", ""), failure=failure,
                        drop_reason="adapter_truncation", db_path=shared.get("DB_PATH") or "")
        try:
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
