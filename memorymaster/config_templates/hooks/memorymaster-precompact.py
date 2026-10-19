"""PreCompact hook: warn ONCE per session, then allow subsequent compactions.

Previously this hook returned {"decision": "block"} unconditionally on every
invocation, which created a permanent deadlock: /compact → blocked → save
claims → /compact → still blocked → /compact → still blocked → ... The
user would get stuck at high-ctx with no escape except external /clear.

New design (2026-04-19): maintain a per-session marker file. First /compact
for a given session_id blocks with the full save-first warning. Subsequent
/compact calls from the same session silently pass through (hook exits 0,
which Claude Code treats as "allow"). Markers expire after 24h so
marathon sessions eventually get re-warned. The stop-hook auto-ingest
(runs every 15 messages) already saves claims regularly, so the one-shot
warning is sufficient.

Residual capture (T-1633, opt-in with MEMORYMASTER_PRECOMPACT_RESIDUAL=1): when the
compaction is allowed to proceed, this hook also extracts a bounded residual (open
decisions, rejected options, unverified assumptions, in-flight card ids, next step)
from the turns about to be summarized, ingests it as candidate claims with
source_agent=precompact-residual, and stores it so the recall hook re-injects it once
on the first prompt after the compaction. It never blocks and never writes to stdout.
PreCompact input: session_id, transcript_path, cwd, trigger (manual|auto), custom_instructions.
"""
import json
import os
import re
import sys
import time
from datetime import datetime

PROJECT_ROOT = "__MEMORYMASTER_PROJECT_ROOT__"
STATE_DIR = os.environ.get("MEMORYMASTER_HOOK_STATE_DIR", "").strip() or os.path.join(
    os.path.expanduser("~"), ".memorymaster", "hook_state"
)
MARKER_TTL_SECONDS = 24 * 3600  # re-warn if marker older than 24h
RESIDUAL_BUDGET_SECONDS = 75  # the installed hook timeout (90s) must outlast this


def _enabled(name):
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no", "off")


def _warn_gate(data):
    """The original warn-once behaviour. Returns True when it emitted the block."""
    maximum_capture = os.environ.get("MEMORYMASTER_PRECOMPACT_BLOCKING", "").strip().lower()
    if maximum_capture in ("", "0", "false", "no", "off"):
        return False
    os.makedirs(STATE_DIR, exist_ok=True)

    session_id = data.get("session_id", "unknown")
    now = time.time()
    # Sanitize session_id for filename — session IDs are typically UUIDs but
    # guard against path traversal / weird chars just in case.
    safe_session = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(session_id))[:64]
    marker_path = os.path.join(STATE_DIR, f"warned_{safe_session}.marker")
    # F-6 fix (overnight audit 2026-05-04): the auto-ingest stop hook STRIPS
    # forbidden chars (re.sub(r"[^a-zA-Z0-9_-]", "", session_id)) when writing
    # its {session_id}_last_save marker. Previously we read it via the RAW
    # session_id, so any non-alphanumeric char in the id (rare but possible)
    # caused a path mismatch → bypass silently failed → /compact deadlock
    # re-emerges (mm-d24c regression). Use the same strip-form auto-ingest uses.
    ai_safe_session = re.sub(r"[^a-zA-Z0-9_-]", "", str(session_id)) or "unknown"
    autosave_path = os.path.join(STATE_DIR, f"{ai_safe_session}_last_save")

    # Bypass: if the auto-ingest stop-hook has fired recently for this session,
    # the session is being auto-saved every 15 messages — manual save warning
    # is redundant. Pass through silently. Fixes the deadlock where /compact
    # is blocked while the user is trying to recover from an unrelated error
    # (e.g. oversized image breaks tool calls → can't manually save → can't compact).
    AUTOSAVE_RECENT_THRESHOLD = 60 * 60  # 1 hour
    try:
        autosave_mtime = os.path.getmtime(autosave_path)
        if (now - autosave_mtime) < AUTOSAVE_RECENT_THRESHOLD:
            try:
                with open(os.path.join(STATE_DIR, "hook.log"), "a") as f:
                    f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] PRE-COMPACT ALLOWED (auto-ingest fired {int(now - autosave_mtime)}s ago) for {safe_session}\n")
            except Exception:
                pass
            return False
    except FileNotFoundError:
        pass
    except Exception:
        pass

    # Has this session already been warned within TTL?
    already_warned = False
    try:
        mtime = os.path.getmtime(marker_path)
        if (now - mtime) < MARKER_TTL_SECONDS:
            already_warned = True
    except FileNotFoundError:
        pass
    except Exception:
        pass

    try:
        with open(os.path.join(STATE_DIR, "hook.log"), "a") as f:
            action = "ALLOWED (already warned this session)" if already_warned else "BLOCKED (first warning for session)"
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] PRE-COMPACT {action} for {safe_session}\n")
    except Exception:
        pass

    if already_warned:
        # Silent pass-through — exit 0 tells Claude Code to allow compaction.
        return False

    # First compaction attempt for this session — block + create marker
    try:
        with open(marker_path, "w") as f:
            f.write(str(now))
    except Exception:
        pass

    sys.stdout.write(json.dumps({
        "decision": "block",
        "reason": "COMPACTION IMMINENT — context will be permanently lost. Before compaction, save ALL important context to MemoryMaster using mcp__memorymaster__ingest_claim. Save: every decision made, every bug root cause, every architecture choice, every gotcha discovered, every constraint learned. Set source_agent to 'claude-session'. Be thorough — after compaction you will NOT remember any of this. NEVER ingest credentials, IPs, or tokens. This is the ONLY block for this session — retry /compact after saving and it will pass through."
    }))
    return True


def _capture_residual(data):
    """Opt-in residual capture; best effort, bounded in time, silent on stdout."""
    if not _enabled("MEMORYMASTER_PRECOMPACT_RESIDUAL") or not isinstance(data, dict):
        return
    try:
        import threading

        sys.path.insert(0, PROJECT_ROOT)
        db_path = os.environ.get("MEMORYMASTER_DEFAULT_DB", "").strip() or os.path.join(PROJECT_ROOT, "memorymaster.db")
        result = {}

        def work():
            try:
                from memorymaster.capture.precompact_residual import capture_residual
                from memorymaster.core.hook_log import log_hook

                ledger = None
                try:
                    from memorymaster.core.capture_control import CaptureLedger, capture_state_path

                    ledger = CaptureLedger(capture_state_path())
                except Exception:
                    ledger = None  # accounting unavailable: the call still proceeds, caps simply cannot be checked
                spool = os.environ.get("MEMORYMASTER_WAL_DISCIPLINE", "0").strip() not in ("0", "false", "False", "no", "off", "")
                result.update(capture_residual(data, db_path=db_path, spool=spool, ledger=ledger))
                log_hook("precompact", "residual", **{k: v for k, v in result.items() if k != "note"})
                sys.stderr.write(f"[MemoryMaster] precompact residual: {result.get('items', 0)} item(s) via {result.get('note', '')}\n")
            except Exception as exc:
                result["error"] = type(exc).__name__

        worker = threading.Thread(target=work, name="memorymaster-precompact-residual", daemon=True)
        worker.start()
        worker.join(RESIDUAL_BUDGET_SECONDS)
    except Exception:
        pass  # a failed residual must never break the compaction


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}
    if _warn_gate(data):
        return  # compaction is blocked; the retry will capture the residual
    _capture_residual(data)


if __name__ == "__main__":
    main()
