"""Thin stdin/plain-text recall adapter for OpenCode's system context hooks.

The plugin bounds the entire subprocess to three seconds. Ranking, scope,
sensitivity and token budgets belong to the same hook_recall core Claude uses.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from memorymaster.core.text_safety import sanitize_text
from memorymaster.recall.delivery import (
    brief_pending, confirm_turn, defer_payload, is_automated, mark_brief_delivered, recall_block,
)
from memorymaster.recall.hook_policy import is_trivial_prompt
from memorymaster.recall.machine_traffic import machine_skip
from memorymaster.recall.recall_format import active_task_text, apply_format, format_mode
from memorymaster.recall.remote import hook_recall


def session_brief(cwd: str, session_id: str) -> str:
    """The project brief, once per session id: OpenCode has no SessionStart event, so its first prompt carries it.
    ``""`` while projections are off, when nothing is published, or after the first delivery."""
    if not brief_pending(session_id):
        return ""
    from memorymaster.profile.project_runtime import session_start_context

    return session_start_context({"cwd": cwd, "session_id": session_id}) or ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", choices=("opencode",), required=True)
    args = parser.parse_args(argv)
    try:
        data = json.loads(sys.stdin.read(65_536) or "{}")
        if not isinstance(data, dict):
            return 0
        prompt = data.get("prompt")
        cwd = data.get("cwd")
        if not isinstance(prompt, str) or not isinstance(cwd, str) or not cwd:
            return 0
        prompt = sanitize_text(prompt)
        if is_automated(prompt) or machine_skip(prompt, args.cli):
            return 0
        data = {"prompt": prompt, "cwd": cwd,
                "session_id": f"{args.cli}:{data.get('session_id') or ''}"}
        db = os.environ.get("MEMORYMASTER_DEFAULT_DB") or str(Path(__file__).resolve().parents[2] / "memorymaster.db")
        brief = session_brief(cwd, data["session_id"])
        trivial = is_trivial_prompt(prompt)
        if trivial and not brief:
            return 0
        data = defer_payload(data)
        context = "" if trivial else hook_recall(prompt, data, db_path=db, timeout=2.5)[0]
        mode = format_mode()
        if context and mode != "current":
            context = apply_format(context, prompt, active_task_text(cwd), mode)
        parts = [part for part in (brief, context) if part]
        block, failure = (recall_block("\n".join(parts)) if parts else ""), "not_emitted"
        try:
            if block:
                sys.stdout.write(block)
                sys.stdout.flush()
            if brief:
                mark_brief_delivered(data["session_id"])
        except Exception:  # the plugin is gone: the claims (and the brief) stay retryable (T-2275)
            block, failure = "", "write_failure"
        if not trivial:
            confirm_turn(data, args.cli, emitted=block, query=prompt, failure=failure, db_path=db)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
