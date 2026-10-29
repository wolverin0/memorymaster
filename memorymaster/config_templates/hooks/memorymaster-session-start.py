#!/usr/bin/env python3
"""Inject MemoryMaster context at session startup.

Fires on SessionStart (startup|resume). Pulls from memorymaster.db:
  1. Last 5 CONFIRMED claims in current project scope (by created_at); with
     the S8 SESSION Jev surface live, the top 5 of the 30 most recent confirmed
     public claims by relevance to the project instead
  2. Candidate claims older than 24h still awaiting promotion
(T-1944, audit D3: the 24h activity line, wiki articles and compiled user
profile are no longer injected - none changes what the model does, and the
profile already lives in the operator's global instructions. Claims are shown
whole or dropped, never cut mid-sentence.)

Returns hookSpecificOutput.additionalContext so Claude starts every session
with MemoryMaster state instead of a blank slate. Graceful degradation: if
DB missing or any query fails, the hook exits silently.

Scope is resolved from CWD through the shared Git-aware scope helper.
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

MM_ROOT = Path("__MEMORYMASTER_PROJECT_ROOT__")
sys.path.insert(0, str(MM_ROOT))

try:
    from memorymaster.core.text_safety import sanitize_text
except Exception:
    # Installed hooks must still start when their project root is unavailable.
    def sanitize_text(text: str) -> str:
        return re.sub(r"[\ud800-\udfff]", "\ufffd", text)


def _log(event, **kw):
    """Minimal inline hook logger — stdlib-only, never crashes."""
    try:
        log_dir = os.path.join(os.path.expanduser("~"), ".memorymaster", "hook_state")
        os.makedirs(log_dir, exist_ok=True)
        parts = [f"[{datetime.now().strftime('%H:%M:%S')}] hook=session-start event={event}"]
        for k, v in kw.items():
            if v is None:
                continue
            parts.append(f"{k}={v}")
        with open(os.path.join(log_dir, "hook.log"), "a", encoding="utf-8") as fh:
            fh.write(sanitize_text(" ".join(parts)) + "\n")
    except Exception:
        pass


DB_PATH = MM_ROOT / "memorymaster.db"
MAX_CONTEXT_CHARS = 1_500  # T-1944 (audit D3): total injection cap (was 3_000, T-0797 P11); ~1.3 KB typical
MAX_RECENT_CLAIMS = 5
MAX_SESSION_POOL = 30  # S8 SESSION: Jev ranks this many recent confirmed claims
CANDIDATE_REVIEW_HOURS = 24


def _record_injection(context):
    if context and context.strip() and not globals().get("MEMORYMASTER_DEFER_HOOK_EVENT", False):
        try:
            from memorymaster.surfaces.hook_events import append_hook_event

            append_hook_event("claude-code", "recall_injected", 1)
        except ImportError:
            pass


def _derive_scope() -> str:
    """Derive scope from current working directory."""
    try:
        sys.path.insert(0, str(MM_ROOT))
        from memorymaster.core.scope_utils import scope_from_cwd_or_user

        return scope_from_cwd_or_user(os.getcwd())
    except Exception:
        return "project"


_RECENT_CONFIRMED_SQL = """
    SELECT id, text, claim_type, status, created_at
    FROM claims
    WHERE scope = ? AND status = 'confirmed'
    ORDER BY created_at DESC, id DESC
    LIMIT ?
"""


def _query_recent_claims(conn: sqlite3.Connection, scope: str, limit: int = MAX_RECENT_CLAIMS) -> list[dict]:
    """Most recent CONFIRMED claims in scope (never candidate/stale/archived)."""
    try:
        cur = conn.execute(_RECENT_CONFIRMED_SQL, (scope, limit))
        return [
            {
                "id": r[0],
                "text": r[1] or "",
                "type": r[2] or "fact",
                "status": r[3],
                "created_at": r[4],
            }
            for r in cur.fetchall()
        ]
    except sqlite3.Error:
        return []


_SESSION_POOL_SQL = """
    SELECT id, text, claim_type, status, created_at, visibility, subject, predicate, object_value
    FROM claims
    WHERE scope = ? AND status = 'confirmed' AND COALESCE(visibility, 'public') = 'public'
    ORDER BY created_at DESC, id DESC
    LIMIT ?
"""


def _query_session_pool(conn: sqlite3.Connection, scope: str, limit: int = MAX_SESSION_POOL) -> list[dict]:
    """S8 Jev pool: the most recent confirmed claims that may leave the machine.

    Recall's egress policy (``jev_surfaces.egress_allowed``: public and not
    flagged by the sensitivity scan, on the full text). A claim outside the
    pool is never sent; if it is among the legacy five it keeps its slot.
    """
    try:
        from types import SimpleNamespace

        from memorymaster.recall.jev_surfaces import egress_allowed

        rows = conn.execute(_SESSION_POOL_SQL, (scope, limit)).fetchall()
        pool = []
        for r in rows:
            claim = SimpleNamespace(visibility=r[5] or "public", text=r[1] or "", subject=r[6],
                                    predicate=r[7], object_value=r[8])
            if egress_allowed(claim):
                pool.append({"id": r[0], "text": r[1] or "", "type": r[2] or "fact",
                             "status": r[3], "created_at": r[4]})
        return pool
    except Exception:
        return []


def _query_candidate_claims(conn: sqlite3.Connection, scope: str) -> int:
    try:
        threshold = (
            datetime.now(timezone.utc) - timedelta(hours=CANDIDATE_REVIEW_HOURS)
        ).isoformat()
        cur = conn.execute(
            """
            SELECT COUNT(*) FROM claims
            WHERE scope = ? AND status = 'candidate' AND created_at < ?
            """,
            (scope, threshold),
        )
        row = cur.fetchone()
        return row[0] if row else 0
    except sqlite3.Error:
        return 0


def _format_context(scope: str, claims: list[dict], candidates: int) -> str:
    """Build the SessionStart context, capped at MAX_CONTEXT_CHARS in total (T-0797 P11, D3 in T-1944).

    Scope header + candidate warning + footer are fixed; claims are appended whole, in order, while they fit.
    A claim that does not fit is dropped, never cut (the footer points at query_memory for the rest).
    """
    lines = [f"[MemoryMaster session context — scope: {scope}]"]

    if candidates:
        lines.append(
            f"WARNING: {candidates} candidate claim(s) older than "
            f"{CANDIDATE_REVIEW_HOURS}h awaiting steward review "
            "(run `mcp__memorymaster__run_cycle` or ignore)"
        )

    footer = (
        "\nUse `mcp__memorymaster__query_memory` before architectural "
        "decisions; use `ingest_claim` after learning something non-obvious."
    )
    header = "\nRecent claims in this scope:"
    used = len("\n".join(lines)) + len(footer)
    claim_lines = []
    for c in claims:
        line = f"  - #{c['id']} [{c['type']}/{c['status']}] {c['text']}"
        cost = len(line) + 1 + (0 if claim_lines else len(header) + 1)  # +1 for the joining newline
        if used + cost <= MAX_CONTEXT_CHARS:
            claim_lines.append(line)
            used += cost
    if claim_lines:
        lines.extend([header, *claim_lines])
    return "\n".join(lines) + footer


def _jev_session_active() -> bool:
    try:
        from memorymaster.recall.jev_surfaces import SESSION_SURFACE, surface_active

        return surface_active(SESSION_SURFACE)
    except Exception:
        return False


def _jev_session_claims(data: dict, scope: str, claims: list[dict], pool: list[dict]) -> list[dict]:
    """S8 SESSION: Jev ranks the recent confirmed pool; legacy = the 5 most recent.

    Returns ``claims`` unchanged unless the engine took Jev's live action (mode
    off/shadow, missing key, timeout, any error -> legacy, logged by the engine).
    A legacy claim outside the pool (private or sensitive) is never sent and
    keeps its slot in the live block too (B1).
    """
    if not pool:
        return claims
    pooled = {c["id"] for c in pool}
    try:
        from memorymaster.recall import jev_surfaces as jev

        chosen = jev.decide_session(
            jev.project_label(cwd=data.get("cwd"), scope=scope),
            [jev.RecallCandidate(c["id"], c["text"][:180]) for c in pool],
            legacy_ids=[c["id"] for c in claims],
            passthrough_ids=[c["id"] for c in claims if c["id"] not in pooled],
            session_key=jev.decision_session_key(data.get("session_id")),
            scope=scope,
        )
    except Exception:
        return claims
    by_id = {**{c["id"]: c for c in pool}, **{c["id"]: c for c in claims}}
    if not chosen or any(cid not in by_id for cid in chosen):
        return claims
    return [by_id[cid] for cid in chosen]


def main():
    data: dict = {}
    # Read stdin (may be empty for SessionStart)
    try:
        data = json.loads(sys.stdin.read() or "{}")
        if not isinstance(data, dict):
            data = {}
        sys.path.insert(0, str(MM_ROOT))
        from memorymaster.recall.delivery import reset_session
        reset_session(data.get("session_id", ""))
    except Exception:
        pass

    # Read the switch without the package: when the consumer is enabled, even an import
    # failure must withhold legacy delivery; when it is off, the hook degrades as before.
    project_enabled = os.environ.get("MEMORYMASTER_PROJECT_PROJECTION_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    try:
        from memorymaster.profile.project_runtime import session_start_context

        context = session_start_context(data)
        if context is not None:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart", "additionalContext": context}}))
            sys.stdout.flush()
            _record_injection(context)
            sys.exit(0)
    except Exception as exc:
        # An enabled project consumer must never fall through to less restrictive legacy SQL.
        if project_enabled:
            _log("project-consumer-error", cause=type(exc).__name__)
            sys.exit(0)

    if not DB_PATH.exists():
        _log("skip", reason="db-missing")
        sys.exit(0)

    try:
        scope = _derive_scope()
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2.0)
        try:
            claims = _query_recent_claims(conn, scope)
            pool = _query_session_pool(conn, scope) if _jev_session_active() else []
            candidates = _query_candidate_claims(conn, scope)
        finally:
            conn.close()
    except Exception as e:
        _log("error", message=str(e)[:200])
        sys.exit(0)

    claims = _jev_session_claims(data, scope, claims, pool)

    # Skip if there's truly nothing to say
    if not claims and not candidates:
        _log("skip", reason="empty", scope=scope)
        sys.exit(0)

    _log("injected", scope=scope, claims=len(claims), candidates=candidates)

    context = _format_context(scope, claims, candidates)
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": sanitize_text(context),
        }
    }
    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()
    _record_injection(context)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
