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

from memorymaster.core.text_safety import fit_brief, sanitize_text
from memorymaster.recall.delivery import is_automated, recall_block
from memorymaster.recall.hook_policy import is_trivial_prompt
from memorymaster.recall.machine_traffic import machine_skip
from memorymaster.recall.recall_format import active_task_text, apply_format, format_mode
from memorymaster.recall.remote import hook_recall


BRIEF_PREFIX = "[MemoryMaster project brief]\n"


def session_brief(data: dict, cli: str) -> str:
    """The project brief for a session's first prompt ("" when the flags are off or nothing is published)."""
    from memorymaster.recall.machine_traffic import filter_enabled, internal_run
    cwd, session = data.get("cwd"), data.get("session_id")
    if not isinstance(cwd, str) or not cwd or not isinstance(session, str) or not session:
        return ""
    if filter_enabled(cli) and internal_run():
        return ""
    from memorymaster.profile.project_runtime import session_start_context

    context = session_start_context({"cwd": cwd, "session_id": f"{cli}:{session}"})
    if not context or not context.strip():
        return ""
    return BRIEF_PREFIX + fit_brief(sanitize_text(context))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", choices=("opencode",), required=True)
    parser.add_argument("--session-start", action="store_true", help="print the project brief instead of a recall")
    args = parser.parse_args(argv)
    try:
        data = json.loads(sys.stdin.read(65_536) or "{}")
        if not isinstance(data, dict):
            return 0
        if args.session_start:
            sys.stdout.write(session_brief(data, args.cli))
            sys.stdout.flush()
            return 0
        prompt = data.get("prompt")
        cwd = data.get("cwd")
        if not isinstance(prompt, str) or not isinstance(cwd, str) or not cwd:
            return 0
        prompt = sanitize_text(prompt)
        if is_automated(prompt) or is_trivial_prompt(prompt) or machine_skip(prompt, args.cli):
            return 0
        data = {"prompt": prompt, "cwd": cwd,
                "session_id": f"{args.cli}:{data.get('session_id') or ''}"}
        db = os.environ.get("MEMORYMASTER_DEFAULT_DB") or str(Path(__file__).resolve().parents[2] / "memorymaster.db")
        context, _via = hook_recall(prompt, data, db_path=db, timeout=2.5)
        mode = format_mode()
        if context and mode != "current":
            context = apply_format(context, prompt, active_task_text(cwd), mode)
        if context:
            sys.stdout.write(recall_block(context))
            sys.stdout.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
