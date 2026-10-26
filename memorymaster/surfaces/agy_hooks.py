"""Thin Antigravity lifecycle adapters: SessionStart recall and Stop capture."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path, PureWindowsPath

from memorymaster.surfaces.hook_events import append_hook_event

INTERNAL_ENV = "MEMORYMASTER_AGY_HOOK_INTERNAL"


def _internal() -> bool:
    if os.environ.get(INTERNAL_ENV) == "1" or os.environ.get("AGY_BATCH_PROFILE"):
        return True
    # Older MemoryMaster clients isolated HOME without setting a batch marker.
    return any(".gemini-batch" in value.replace("\\", "/").split("/")
               for value in (os.environ.get("HOME", ""), os.environ.get("USERPROFILE", "")))


def normalize_payload(payload: dict) -> dict | None:
    """Require one explicit workspace; the hook process cwd is the config directory."""
    paths = payload.get("workspacePaths")
    session = payload.get("conversationId")
    if not isinstance(paths, list) or len(paths) != 1 or not isinstance(paths[0], str):
        return None
    cwd = paths[0].strip()
    if not cwd or not (Path(cwd).is_absolute() or PureWindowsPath(cwd).is_absolute()):
        return None
    if not isinstance(session, str) or not session.strip():
        return None
    prompt = payload.get("lastUserInput", "")
    if not isinstance(prompt, str):
        return None
    return {"cwd": cwd, "session_id": session, "prompt": prompt,
            "model": payload.get("modelName"), "transcript_path": payload.get("transcriptPath")}


def run_hook(event: str, payload: dict, *, db_path: str) -> str:
    """Return strict agy JSON or silence; errors never veto the agent's turn."""
    if event not in {"SessionStart", "Stop"} or _internal():
        return ""
    data = normalize_payload(payload)
    if data is None:
        return ""
    from memorymaster.recall.delivery import is_automated, recall_block

    if is_automated(data["prompt"]):
        return ""
    previous = os.environ.get(INTERNAL_ENV)
    os.environ[INTERNAL_ENV] = "1"
    try:
        # Dependencies can print diagnostics. Nothing except our strict envelope
        # may reach agy's protojson parser, including on a failing dependency.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            if event == "SessionStart":
                from memorymaster.core.scope_utils import scope_from_cwd
                from memorymaster.recall.remote import hook_recall

                query = data["prompt"].strip() or f"Project context for {scope_from_cwd(data['cwd'])}"
                context, _via = hook_recall(query, data, db_path=db_path)
                if not context or not context.strip():
                    return ""
                output = json.dumps({"injectSteps": [{"ephemeralMessage": recall_block(context)}]})
                count = context.count("\n- [mm-") or 1
            else:
                value = os.environ.get("MEMORYMASTER_DREAM_ENABLED")
                if value is not None and value.strip().lower() in {"0", "false", "no", "off", ""}:
                    return ""
                if not isinstance(data["transcript_path"], str) or not data["transcript_path"]:
                    return ""
                from memorymaster.core.capture_control import capture_state_path
                from memorymaster.dreaming.capture import capture_hook_payload
                from memorymaster.dreaming.ledger import DreamLedger

                result = capture_hook_payload(payload, provider="antigravity", ledger=DreamLedger(capture_state_path()))
                count = result.get("queued", 0)
                if not count:
                    return ""
                output = ""
            append_hook_event("agy", event, count)
        return output
    except Exception:
        return ""
    finally:
        if previous is None:
            os.environ.pop(INTERNAL_ENV, None)
        else:
            os.environ[INTERNAL_ENV] = previous


def main(event: str, *, db_path: str) -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if isinstance(payload, dict):
            output = run_hook(event, payload, db_path=db_path)
            if output:
                sys.stdout.write(output)
                sys.stdout.flush()
    except Exception:
        pass
    return 0
