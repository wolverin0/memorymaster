"""Ask the warm shared MCP server for hook recall instead of recalling cold (T-0594).

A per-prompt hook process spent ~1.5 s of a ~2.1 s run opening the authoritative
DB and building caches; the same recall in the long-lived shared server takes
~0.05 s. Standard library only, so importing it costs the hook nothing.

``remote_recall`` returns ``RemoteRecall(ctx, reachable)``:
- ``ctx`` is the recall text (possibly empty) when the server answered;
- ``reachable`` is False only when nothing reached the server (connection
  refused or not configured), so the caller may still recall locally. A
  timeout or a server error means the server may be busy: the caller skips
  recall for this prompt rather than adding a cold recall on top.
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import NamedTuple

from memorymaster.core.shared_mcp import DEFAULT_URL, resolve_token
from memorymaster.core.text_safety import sanitize_text
from memorymaster.recall import hook_policy

HOOK_RECALL_PATH = "/hook/recall"
DEFAULT_TIMEOUT_SECONDS = 4.0
FILE_HINT_TIMEOUT_SECONDS = 1.5  # the edit's PreToolUse hook is registered for 5 s and must never be the slow step


class RemoteRecall(NamedTuple):
    ctx: str | None
    reachable: bool


class RemoteHint(NamedTuple):
    claims: list | None
    reachable: bool


def hook_recall_url(environ=os.environ) -> str:
    base = (environ.get("MEMORYMASTER_SHARED_MCP_URL") or DEFAULT_URL).strip() or DEFAULT_URL
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return base.rstrip("/") + HOOK_RECALL_PATH


def _post(payload: dict, *, timeout: float, environ, opener) -> tuple[dict | None, bool]:
    """POST ``payload`` to the hook route: ``(answer, reachable)``, ``answer`` None when none came back."""
    if (environ.get("MEMORYMASTER_HOOK_RECALL_REMOTE") or "1").strip().lower() in {"0", "false", "no", "off"}:
        return None, False
    token = resolve_token(environ)
    if not token:
        return None, False
    body = sanitize_text(json.dumps(payload, ensure_ascii=False)).encode("utf-8")
    request = urllib.request.Request(
        hook_recall_url(environ),
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with opener(request, timeout=timeout) as response:
            answer = json.loads(response.read().decode("utf-8") or "{}")
        return answer, True
    except urllib.error.HTTPError as exc:
        # 404: an older server without the route; recalling locally is safe.
        return None, exc.code != 404
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        # Only a timeout proves the server is up but busy; anything else
        # (refused, unresolvable) means it never got the request.
        return None, isinstance(reason, (TimeoutError, socket.timeout))


def remote_recall(
    query: str,
    hook_data: dict | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    environ=os.environ,
    opener=urllib.request.urlopen,
) -> RemoteRecall:
    answer, reachable = _post({"query": query, "hook_data": hook_data}, timeout=timeout, environ=environ, opener=opener)
    if answer is None:
        return RemoteRecall(None, reachable)
    return RemoteRecall(sanitize_text(str(answer.get("ctx") or "")), True)


def remote_file_hint(
    lookup: str,
    cwd: str,
    tail: list[str],
    *,
    timeout: float = FILE_HINT_TIMEOUT_SECONDS,
    environ=os.environ,
    opener=urllib.request.urlopen,
) -> RemoteHint:
    """Candidate claims for one file from the warm server (the pre-edit hint), recording nothing.

    The lookup text travels under ``file_hint``, never ``query``: a server that does not know the field must
    not run the prompt recall path (its seen/access/receipt side effects) for an edit. It also never gets
    the session id. ``claims`` is None when no usable answer came back; the caller stays silent then, it
    never recalls cold.
    """
    answer, reachable = _post({"file_hint": {"query": lookup, "tail": tail}, "hook_data": {"cwd": cwd}},
                              timeout=timeout, environ=environ, opener=opener)
    claims = answer.get("claims") if isinstance(answer, dict) else None  # an older server answers {"ctx": ""}
    return RemoteHint(claims if isinstance(claims, list) else None, reachable)


def hook_recall(query: str, hook_data: dict | None, *, db_path: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> tuple[str, str]:
    """Recall for the UserPromptSubmit hook: ``(ctx, via)``, ``via`` in shared/local/skipped_busy/off/trivial.

    ``off``: the hook is switched off for this cwd (``hook_policy``); nothing is recalled.
    ``trivial``: the prompt is an acknowledgement or empty (``hook_policy.is_trivial_prompt``, T-1433).

    The warm shared server first. If nothing reached it, recall locally (the
    pre-T-0594 behaviour). If it was reached but did not answer in time, skip:
    a cold local recall on a busy machine is what used to hit the hook timeout.
    """
    if hook_policy.disabled_for((hook_data or {}).get("cwd")):  # per-lane A/B switch (T-1351)
        return "", "off"
    if hook_policy.is_trivial_prompt(query):  # "ok"/"dale" must not re-inject ~800 tokens (T-1433)
        return "", "trivial"
    answer = remote_recall(query, hook_data, timeout=timeout)
    if answer.ctx is not None:
        return answer.ctx, "shared"
    if answer.reachable:
        return "", "skipped_busy"
    from memorymaster.recall.context_hook import recall

    extra = {"hook_data": hook_data} if hook_data is not None else {}
    return recall(query, db_path=db_path, skip_qdrant=True, **extra) or "", "local"
