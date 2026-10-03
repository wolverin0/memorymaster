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

from memorymaster.surfaces.mcp_stdio_proxy import DEFAULT_URL, resolve_token

HOOK_RECALL_PATH = "/hook/recall"
DEFAULT_TIMEOUT_SECONDS = 4.0


class RemoteRecall(NamedTuple):
    ctx: str | None
    reachable: bool


def hook_recall_url(environ=os.environ) -> str:
    base = (environ.get("MEMORYMASTER_SHARED_MCP_URL") or DEFAULT_URL).strip() or DEFAULT_URL
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return base.rstrip("/") + HOOK_RECALL_PATH


def remote_recall(
    query: str,
    hook_data: dict | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    environ=os.environ,
    opener=urllib.request.urlopen,
) -> RemoteRecall:
    if (environ.get("MEMORYMASTER_HOOK_RECALL_REMOTE") or "1").strip().lower() in {"0", "false", "no", "off"}:
        return RemoteRecall(None, False)
    token = resolve_token(environ)
    if not token:
        return RemoteRecall(None, False)
    body = json.dumps({"query": query, "hook_data": hook_data}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        hook_recall_url(environ),
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
        return RemoteRecall(str(payload.get("ctx") or ""), True)
    except urllib.error.HTTPError as exc:
        # 404: an older server without the route; recalling locally is safe.
        return RemoteRecall(None, exc.code != 404)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        # Only a timeout proves the server is up but busy; anything else
        # (refused, unresolvable) means it never got the request.
        return RemoteRecall(None, isinstance(reason, (TimeoutError, socket.timeout)))


def hook_recall(query: str, hook_data: dict | None, *, db_path: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> tuple[str, str]:
    """Recall for the UserPromptSubmit hook: ``(ctx, via)``, ``via`` in shared/local/skipped_busy.

    The warm shared server first. If nothing reached it, recall locally (the
    pre-T-0594 behaviour). If it was reached but did not answer in time, skip:
    a cold local recall on a busy machine is what used to hit the hook timeout.
    """
    answer = remote_recall(query, hook_data, timeout=timeout)
    if answer.ctx is not None:
        return answer.ctx, "shared"
    if answer.reachable:
        return "", "skipped_busy"
    from memorymaster.recall.context_hook import recall

    extra = {"hook_data": hook_data} if hook_data is not None else {}
    return recall(query, db_path=db_path, skip_qdrant=True, **extra) or "", "local"
