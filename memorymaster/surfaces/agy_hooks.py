"""Thin Antigravity lifecycle adapters: SessionStart and per-prompt (PreInvocation) recall, Stop capture."""

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


def _workspace(paths: object) -> str | None:
    """The one explicit workspace; with several roots, the first that resolves to a project scope."""
    if not isinstance(paths, list):
        return None
    roots = [p.strip() for p in paths if isinstance(p, str) and p.strip()
             and (Path(p.strip()).is_absolute() or PureWindowsPath(p.strip()).is_absolute())]
    if len(roots) == 1 and len(paths) == 1:
        return roots[0]
    from memorymaster.core.scope_utils import resolve_workspace_scope

    for root in roots:
        if resolve_workspace_scope(root, strict=False) not in (None, "global"):
            return root
    return None


def normalize_payload(payload: dict) -> dict | None:
    """Require an explicit workspace; the hook process cwd is the config directory."""
    session = payload.get("conversationId")
    cwd = _workspace(payload.get("workspacePaths"))
    if cwd is None:
        return None
    if not isinstance(session, str) or not session.strip():
        return None
    prompt = payload.get("lastUserInput", "")
    if not isinstance(prompt, str):
        return None
    return {"cwd": cwd, "session_id": session, "prompt": prompt,
            "model": payload.get("modelName"), "transcript_path": payload.get("transcriptPath")}


def _project_brief(data: dict) -> str | None:
    """The SessionStart project brief, fitted to the shared budget; None = legacy recall, "" = say nothing.

    Same contract as the Claude consumer: flags off -> None (unchanged behaviour); an enabled consumer that fails
    never falls through to the less restrictive recall.
    """
    enabled = os.environ.get("MEMORYMASTER_PROJECT_PROJECTION_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    try:
        from memorymaster.core.text_safety import fit_brief
        from memorymaster.profile.project_runtime import session_start_context

        context = session_start_context({"cwd": data["cwd"], "session_id": data["session_id"]})
        return None if context is None else (fit_brief(context) if context.strip() else "")
    except Exception:
        return "" if enabled else None


def run_hook(event: str, payload: dict, *, db_path: str) -> str:
    """Return strict agy JSON or silence; errors never veto the agent's turn."""
    if event not in {"SessionStart", "PreInvocation", "Stop"} or _internal():
        return ""
    data = normalize_payload(payload)
    if data is None:
        return ""
    from memorymaster.recall.delivery import is_automated, recall_block

    from memorymaster.recall.machine_traffic import machine_skip

    if is_automated(data["prompt"]):
        return ""
    if event != "Stop" and machine_skip(data["prompt"], "agy", session=data["session_id"][:16]):
        return ""  # recall only: Stop capture is not recall (T-2273)
    previous = os.environ.get(INTERNAL_ENV)
    os.environ[INTERNAL_ENV] = "1"
    try:
        # Dependencies can print diagnostics. Nothing except our strict envelope
        # may reach agy's protojson parser, including on a failing dependency.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            brief = _project_brief(data) if event == "SessionStart" else None
            if brief is not None:
                if not brief:
                    return ""
                output = json.dumps({"injectSteps": [{"ephemeralMessage": brief}]})
                count = 1
            elif event in {"SessionStart", "PreInvocation"}:
                from memorymaster.core.scope_utils import scope_from_cwd
                from memorymaster.recall.remote import hook_recall

                if event == "PreInvocation" and not data["prompt"].strip():
                    return ""  # per-prompt recall needs a prompt; only SessionStart falls back to the scope
                query = data["prompt"].strip() or f"Project context for {scope_from_cwd(data['cwd'])}"
                context, _via = hook_recall(query, data, db_path=db_path)
                if not context or not context.strip():
                    return ""
                output = json.dumps({"injectSteps": [{"ephemeralMessage": recall_block(context)}]})
                count = 1
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
