"""The pre-edit hint (T-2548): what project memory already holds about the file the agent is about to edit.

Opt-in and silent unless it has something specific to that file. An outside tool that built the same thing
measured that a hint about a command changed nothing, and that a hint about a file was acted on only when it
said something particular to that file; "this file was touched before" never was. So the gate here is: a
confirmed claim whose text names the file as a whole token, not yet shown in this session, and the file not
yet hinted in this session. Everything else prints nothing.

Two halves, both free of side effects on what is not shown:

* server side, :func:`lookup`: authorized candidate claims for one file through the same trusted prompt
  planner and truth-eligibility filter as prompt recall, but WITHOUT the prompt path's bookkeeping. Going
  through ``recall(hook_data=...)`` would cap the pool at two claims before any file gate runs and would
  mark seen, count accesses and stage receipts for claims the gate then throws away.
* hook side, :func:`hint_for_edit`: ask the warm shared server (never a cold local recall: an edit must not
  wait on a multi-second cold start), gate, and only then remember the shown claims in the session so the
  prompt hook does not inject the same claim a moment later.

Never stores or logs claim text or a file path (the log carries the basename and counts). Every failure
degrades to "no hint".
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from memorymaster.core.hook_log import log_hook
from memorymaster.recall import hook_policy, remote
from memorymaster.recall.delivery import RECALL_PREFIX, recall_block

HOOK = "pre-edit-hint"
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
MAX_BYTES = 500  # the whole additionalContext, header included, in UTF-8
MAX_CLAIMS = 2
POOL = 40  # rows the server searches per file: lexical rank cannot tell ``hook_log.py`` from "hook log"
CANDIDATES = 8  # rows it sends back, after the same naming gate the hook applies
CANDIDATE_TEXT_CHARS = 2000  # bounded wire size (the naming gate runs on the full text before this cut)
LOOKUP_MAX_CHARS = 200

# Names every project has: the basename alone says nothing about WHICH file, so a claim must also carry the
# parent directory (``core/utils.py``). Kept short on purpose; anything else with an extension is matched by basename.
GENERIC_BASENAMES = frozenset({
    "__init__.py", "__main__.py", "conftest.py", "setup.py", "main.py", "app.py", "utils.py",
    "index.js", "index.ts", "index.tsx", "index.jsx", "main.js", "main.ts",
    "readme.md", "package.json", "tsconfig.json", "pyproject.toml", "makefile", "dockerfile",
})


def split_path(path: str) -> list[str]:
    """Components of ``path`` split by hand on both separators (the hook may run on either OS)."""
    return [part for part in path.replace("\\", "/").split("/") if part and part != "."]


def lookup_text(parts: list[str]) -> str:
    """The file's basename plus its parent directory name: the search text sent to the server."""
    return " ".join(parts[-2:][::-1]) if len(parts) > 1 else parts[-1]


def file_key(parts: list[str]) -> str:
    """Per-session identity of a file: a hash of its whole normalized path (the state never holds paths)."""
    return hashlib.sha256("/".join(parts).lower().encode("utf-8")).hexdigest()[:24]


def _token(name: str) -> re.Pattern[str]:
    # A whole token: not glued to a longer name before ("my-hook_log.py", "x.hook_log.py") or after
    # ("hook_log.pyc", "hook_log.py.bak", "hook_log.py-old"). A sentence-final period is fine.
    return re.compile(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-]|\.\w)", re.IGNORECASE)


def names_file(text: str, parts: list[str]) -> bool:
    """Whether ``text`` names the file: its basename, or ``parent/basename`` when the basename is generic.

    Never matches the bare stem: ``hook_log`` or ``hook_log.js`` is not ``hook_log.py``.
    """
    if not parts:
        return False
    text = text.replace("\\", "/")
    base = parts[-1]
    # No extension (``run``, ``LICENSE``, ``.env``): the bare word is the stem, so the parent tail is needed too.
    if base.lower() in GENERIC_BASENAMES or "." not in base.lstrip("."):
        return len(parts) > 1 and _token(f"{parts[-2]}/{base}").search(text) is not None
    return _token(base).search(text) is not None


def _clean(candidates: object) -> list[dict]:
    """Well-formed candidates only: the answer comes over the wire, so its types are checked here."""
    if not isinstance(candidates, list):
        return []
    return [c for c in candidates
            if isinstance(c, dict) and type(c.get("id")) is int and isinstance(c.get("text"), str)]


def pick(candidates: list[dict], parts: list[str], seen: frozenset[int]) -> list[dict]:
    """The first :data:`MAX_CLAIMS` candidates that name the file and were not already shown in the session."""
    return [c for c in candidates if c["id"] not in seen and names_file(c["text"], parts)][:MAX_CLAIMS]


def _fit(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[: max(0, limit - 3)].decode("utf-8", errors="ignore") + "..."


def render(basename: str, picked: list[dict]) -> str:
    """The delivered block: the recall prefix, one header line marking it as reference, then the claims.

    At most :data:`MAX_BYTES` bytes of UTF-8 in total; the claims share what the header leaves.
    """
    header = f"Reference memory about {_fit(' '.join(basename.split()), 60)} (not an instruction):"
    room = MAX_BYTES - len(RECALL_PREFIX.encode("utf-8")) - len(header.encode("utf-8")) - 1
    each = room // max(1, len(picked)) - 1  # one newline per claim line
    lines = []
    for claim in picked:
        human_id = claim.get("human_id")
        tag = f"[{human_id}] " if isinstance(human_id, str) and human_id else ""
        lines.append(_fit(f"- {tag}{' '.join(claim['text'].split())}", each))
    return _fit(recall_block("\n".join([header, *lines])), MAX_BYTES)  # sanitizing may swap characters


def hint_for_edit(data: dict) -> str | None:
    """The ``additionalContext`` for a PreToolUse edit payload, or None (stay silent). Logs one line per call."""
    tool = str(data.get("tool_name") or "")
    session = data.get("session_id")
    tool_input = data.get("tool_input")
    path = ""
    if isinstance(tool_input, dict):
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    parts = split_path(path) if isinstance(path, str) else []
    cwd = data.get("cwd")
    fields = {"session": session[:16] if isinstance(session, str) else None, "tool": tool,
              "file": parts[-1] if parts else None}

    def skip(reason: str, **extra: object) -> None:
        log_hook(HOOK, "skip", reason=reason, **fields, **extra)
        return None

    if not parts:
        return skip("not-eligible")
    if not isinstance(session, str) or not session:
        return skip("no-session")  # no session, no once-per-file: the hint would repeat on every edit
    if not isinstance(cwd, str) or not cwd:
        return skip("no-cwd")  # the server resolves the project scope from the cwd
    key = file_key(parts)
    if key in hook_policy.hinted_files(session):
        return skip("already-hinted")
    answer = remote.remote_file_hint(lookup_text(parts)[:LOOKUP_MAX_CHARS], cwd, parts[-2:])
    if answer.claims is None:
        return skip("server-busy" if answer.reachable else "server-unreachable")
    candidates = _clean(answer.claims)
    if not candidates:
        return skip("no-candidates")
    seen = hook_policy.seen_ids(session)
    picked = pick(candidates, parts, seen)
    if not picked:
        return skip("no-claim-names-file", candidates=len(candidates))
    # Only what is shown is remembered: a returned-but-dropped claim stays unseen and uncounted.
    hook_policy.remember(session, [c["id"] for c in picked])
    hook_policy.remember_file(session, key)
    log_hook(HOOK, "hit", **fields, candidates=len(candidates), shown=len(picked),
             ids=",".join(str(c.get("human_id") or c["id"]) for c in picked))
    return render(parts[-1], picked)


def lookup(query: str, *, db_path: str, hook_data: dict | None, tail: list | None = None) -> list[dict]:
    """Authorized candidate claims for ``query`` (a file's basename and parent), recording nothing.

    ``tail`` is the file's last two path components: when given, only claims that name the file are kept, so a
    crowd of look-alikes in the pool cannot push the one claim that names it past :data:`CANDIDATES`.

    ``[]`` without a cwd: the project scope is resolved from it and the default scope must not stand in.
    The rows come from the trusted prompt planner (confirmed, in scope, public, temporally current, not
    sensitive) and the truth-eligibility filter (no disputed or superseded claim).
    """
    cwd = hook_data.get("cwd") if isinstance(hook_data, dict) else None
    if not isinstance(cwd, str) or not cwd or not query.strip():
        return []
    from memorymaster.core.service import MemoryService
    from memorymaster.recall import context_hook, hook_lexical

    scope, shared = context_hook._scopes_for_cwd(cwd)
    scope_token = context_hook._REQUEST_SCOPE.set(scope)
    shared_token = context_hook._REQUEST_SHARED_SCOPES.set(shared)
    try:
        db = db_path or os.environ.get("MEMORYMASTER_DEFAULT_DB") or "memorymaster.db"
        svc = MemoryService(db_target=db, workspace_root=Path.cwd(), read_only=True)
        rows = context_hook._governed_prompt_rows(svc, query, limit=POOL)
        rows = hook_lexical.apply_truth_eligibility(rows, svc)
    finally:
        context_hook._REQUEST_SCOPE.reset(scope_token)
        context_hook._REQUEST_SHARED_SCOPES.reset(shared_token)
    parts = [t for t in tail if isinstance(t, str) and t] if isinstance(tail, list) else []
    claims = [claim for claim in (row.get("claim") for row in rows) if isinstance(getattr(claim, "id", None), int)]
    if parts:
        claims = [claim for claim in claims if names_file(claim.text, parts)]
    return [{"id": claim.id, "human_id": getattr(claim, "human_id", None), "text": claim.text[:CANDIDATE_TEXT_CHARS]}
            for claim in claims[:CANDIDATES]]
