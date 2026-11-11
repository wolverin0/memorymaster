"""Best-effort, bounded recall delivery state; never stores memory content."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import uuid
from typing import Callable

from memorymaster.core.text_safety import sanitize_text
from memorymaster.recall import hook_policy

AUTOMATED_PREFIXES = (
    "[SYSTEM NOTIFICATION - NOT USER INPUT]", "<task-notification>",
    "AUTO-SAVE checkpoint", "FLEET agents=",
)
REPEAT_WINDOW_SECONDS = 300
RECALL_PREFIX = "[MemoryMaster recall]\n"


def is_automated(prompt: str) -> bool:
    return prompt.lstrip().startswith(AUTOMATED_PREFIXES)


def _state_path(session_id: str, state_dir: Path | None = None) -> Path:
    directory = state_dir or Path(os.environ.get(
        "MEMORYMASTER_RECALL_STATE_DIR",
        str(Path.home() / ".memorymaster" / "hook_state" / "recall_delivery"),
    ))
    name = hashlib.sha256(sanitize_text(session_id).encode("utf-8")).hexdigest()
    return directory / (name + ".json")


def reset_session(session_id: str, *, state_dir: Path | None = None) -> None:
    """SessionStart (including compact/resume) permits unchanged context again."""
    if not isinstance(session_id, str) or not session_id:
        return
    try:
        _state_path(session_id, state_dir).unlink(missing_ok=True)
    except OSError:
        pass
    hook_policy.forget(session_id)  # the claims injected before are gone from the context too


def _recent(path: Path, fingerprint: str, now: float) -> bool:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        age = now - float(state["delivered_at"])
        return state["fingerprint"] == fingerprint and 0 <= age < REPEAT_WINDOW_SECONDS
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _remember(path: Path, fingerprint: str, now: float) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({"fingerprint": fingerprint, "delivered_at": now}, handle)
        os.replace(temporary, path)
    except OSError:
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def recall_block(context: str) -> str:
    """The exact text the recall hook delivers for a rendered recall ``context``."""
    return RECALL_PREFIX + sanitize_text(context)


def _session_path(data: dict, state_dir: Path | None) -> Path | None:
    session = data.get("session_id")
    return _state_path(session, state_dir) if isinstance(session, str) and session else None


def _fingerprint(data: dict, context: str) -> str:
    return hashlib.sha256(sanitize_text(json.dumps(
        [data.get("cwd", ""), context], ensure_ascii=False,
    )).encode("utf-8")).hexdigest()


def would_deliver(data: dict, context: str, *, state_dir: Path | None = None,
                  now: float | None = None) -> bool:
    """Whether :func:`deliver` would write ``context`` now; reads state, never writes it."""
    if not context.strip():
        return False
    now = time.time() if now is None else now
    path = _session_path(data, state_dir)
    return path is None or not _recent(path, _fingerprint(data, context), now)


def record_delivered(data: dict, context: str, *, state_dir: Path | None = None, now: float | None = None) -> None:
    """Remember ``context`` as delivered: an identical block is suppressed for REPEAT_WINDOW_SECONDS."""
    path = _session_path(data, state_dir)
    if path is not None:
        _remember(path, _fingerprint(data, sanitize_text(context)), time.time() if now is None else now)


def deliver(data: dict, context: str, write: Callable[[str], object], *,
            state_dir: Path | None = None, now: float | None = None, remember: bool = True) -> bool:
    """Record only after output succeeds; state failures never break recall.

    ``remember=False`` leaves the repeat fingerprint to the caller (:func:`record_delivered`), for an
    adapter whose own final write comes after this one (Codex, T-2275).
    """
    context = sanitize_text(context)
    if not context.strip():
        return False
    now = time.time() if now is None else now
    path = _session_path(data, state_dir)
    fingerprint = _fingerprint(data, context)
    if path is not None and _recent(path, fingerprint, now):
        return False
    write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": context,
    }}))
    if path is not None and remember:
        _remember(path, fingerprint, now)
    return True


# --- delivery receipts (T-2275): selected -> packed -> emitted ------------------------------------------
# A claim counts as delivered (session-seen, access recorded) only once the adapter's FINAL emission
# succeeded and the claim survived it. Recall stages the ids (hook_policy.stage_turn); the adapter calls
# confirm_turn after its write. A receipt is one hook.log line of ids and omission reasons: no claim or prompt text.

OMISSION_REASONS = ("session_seen", "slot_cap", "byte_budget", "jev_surface", "repeat_suppressed",
                    "adapter_truncation", "format_dropped", "write_failure", "not_emitted")
RECEIPT_EVENT = "receipt"


def new_turn_id() -> str:
    return uuid.uuid4().hex[:12]


def _ids(values) -> str:
    return ",".join(str(v) for v in values) or "-"


_BULLET_TAG = re.compile(r"^- \[(mm-[0-9A-Za-z.]+(?:~\d+)?)\]", re.MULTILINE)


def defer_payload(data: dict) -> dict:
    """``data`` for a recall whose delivery this adapter confirms (T-2275).

    Carries the turn id the adapter will confirm (``DEFER_KEY``) and the ids its own state dir already holds
    (``seen_ids``): a server resolving another state dir must not re-inject what this hook marked seen.
    """
    payload = {**data, hook_policy.DEFER_KEY: new_turn_id()}
    session = hook_policy.session_id_of(data)
    seen = sorted(hook_policy.seen_ids(session)) if session else []
    if seen:
        payload["seen_ids"] = seen
    return payload


def _resolve_claim_ids(db_path: str, tags: list[str]) -> list[int]:
    """Claim ids of human ids, read-only (the hook never writes the DB); [] when the DB cannot be read."""
    if not db_path or not tags or not Path(db_path).is_file():
        return []
    try:
        with contextlib.closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True,
                                                timeout=1.0)) as conn:
            marks = ",".join("?" * len(tags))
            return [row[0] for row in conn.execute(f"SELECT id FROM claims WHERE human_id IN ({marks})", tags)]
    except sqlite3.Error:
        return []


def _log_receipt(adapter: str, session: str, **fields) -> None:
    from memorymaster.core.hook_log import log_hook

    log_hook("recall", RECEIPT_EVENT, adapter=adapter,
             session=hashlib.sha256(session.encode("utf-8")).hexdigest()[:12], **fields)


def _confirm_unstaged(data: dict, session: str, adapter: str, emitted: str, query: str, db_path: str) -> dict | None:
    """No staged turn for this id (another state dir, a stage that arrived late, a server older than this hook).

    The claims were still emitted, so they are marked seen from the ``[mm-id]`` bullets actually written, or the
    next prompt would inject them again; one ``turn=missing`` receipt says so. Nothing is emitted, nothing to do.
    """
    tags = list(dict.fromkeys(_BULLET_TAG.findall(emitted)))
    if not tags:
        return None
    ids = _resolve_claim_ids(db_path, tags)
    if ids:
        if hook_policy.session_id_of(data):
            hook_policy.remember(session, ids)
        from memorymaster.core import access_recording

        access_recording.spool_accesses(db_path, ids, query)
    _log_receipt(adapter, session, turn="missing", reason="no_staged_turn", hook_turn=hook_policy.turn_id_of(data),
                 tags=len(tags), emitted=_ids(ids))
    return {"turn": "missing", "adapter": adapter, "emitted": ids}


def confirm_turn(data: dict, adapter: str, *, emitted: str, query: str = "", failure: str | None = None,
                 drop_reason: str = "format_dropped", db_path: str = "") -> dict | None:
    """Close the turn recall staged for this prompt (``data[DEFER_KEY]``); returns the receipt, else None.

    ``emitted`` is the text the adapter really wrote ("" when it wrote nothing, with ``failure`` naming why:
    ``write_failure`` or ``repeat_suppressed``). A packed claim whose ``[mm-id]`` tag is missing from a
    non-empty ``emitted`` was cut by the adapter (``drop_reason``, ``adapter_truncation`` for Codex) and stays
    retryable. When nothing was staged under this turn id but claim bullets were emitted, see
    :func:`_confirm_unstaged` (``db_path`` resolves their ids). Never raises: delivery state and telemetry must
    not break a hook.
    """
    try:
        session = data.get("session_id") if isinstance(data, dict) else None
        turn_id = hook_policy.turn_id_of(data)
        if not isinstance(session, str) or not session or turn_id is None:
            return None
        turn = hook_policy.take_turn(session, turn_id)
        if turn is None:
            return _confirm_unstaged(data, session, adapter, emitted, query, db_path)
        packed = [(cid, tag) for cid, tag in turn["packed"]]
        # shortcut: a claim without a human id cannot be located in the text, so it counts as emitted
        # whenever anything was; every claim ingested through the service has one.
        kept = [cid for cid, tag in packed if emitted and (not tag or f"[{tag}]" in emitted)]
        omitted = dict(turn["omitted"])
        for cid, _ in packed:
            if cid not in kept:
                omitted[str(cid)] = (failure or "not_emitted") if not emitted else drop_reason
        if kept:
            if hook_policy.session_id_of(data):
                hook_policy.remember(session, kept)
            from memorymaster.core import access_recording

            access_recording.spool_accesses(turn.get("db_path", ""), kept, query)
        receipt = {"turn": turn["turn"], "adapter": adapter, "ranked": turn.get("ranked_n", 0),
                   "selected": turn["selected"], "packed": [cid for cid, _ in packed], "emitted": kept,
                   "omitted": omitted}
        _log_receipt(adapter, session, turn=receipt["turn"], ranked=receipt["ranked"],
                     selected=_ids(receipt["selected"]), packed=_ids(receipt["packed"]), emitted=_ids(kept),
                     omitted=",".join(f"{cid}:{reason}" for cid, reason in omitted.items()) or "-")
        return receipt
    except Exception:  # noqa: BLE001 — observation and bookkeeping only
        return None
