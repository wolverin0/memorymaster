"""Stop hook: Block-based memory capture (MemPalace-inspired).

Every SAVE_INTERVAL human messages, BLOCK Claude from stopping and force a save.
Uses decision:block + reason as system message. Claude saves to MemoryMaster,
then next Stop fires with stop_hook_active=true → passes through.

Also runs Gemini extraction as fallback for non-block turns.
"""
import json
import os
import sys
import re
import sqlite3
import hashlib
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = "__MEMORYMASTER_PROJECT_ROOT__"
DB_PATH = os.path.join(PROJECT_ROOT, "memorymaster.db")
STATE_DIR = os.path.join(os.path.expanduser("~"), ".memorymaster", "hook_state")
SAVE_INTERVAL = 15  # Block every N human messages

sys.path.insert(0, PROJECT_ROOT)

os.makedirs(STATE_DIR, exist_ok=True)

SENSITIVE = re.compile(
    r"(?i)password\s*(?:is|=|:)\s*\S+|secret[:=]\s*\S{8,}|token[:=]\s*\S{20,}"
    r"|sk-[A-Za-z0-9\-]{20,}|ghp_[A-Za-z0-9]{20,}|192\.168\.\d"
    r"|\d{8,}:[A-Za-z0-9_-]{30,}|ubuntu@|ssh\s+\w+@"
)


def _count_human_messages(transcript_path):
    """Count human messages in JSONL transcript."""
    if not transcript_path or not os.path.exists(transcript_path):
        return 0
    count = 0
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    msg = entry.get("message", entry)
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        content = msg.get("content", "")
                        if isinstance(content, str) and "<command-message>" in content:
                            continue
                        count += 1
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return count


def _get_last_save(session_id):
    """Get last save exchange count for this session."""
    path = os.path.join(STATE_DIR, f"{session_id}_last_save")
    if os.path.exists(path):
        try:
            return int(open(path).read().strip())
        except (ValueError, OSError):
            pass
    return 0


def _set_last_save(session_id, count):
    """Record last save exchange count."""
    path = os.path.join(STATE_DIR, f"{session_id}_last_save")
    try:
        with open(path, "w") as f:
            f.write(str(count))
    except OSError:
        pass


def _run_gemini_extraction(transcript_path, cwd):
    """Fallback: run Gemini extraction on non-block turns."""
    try:
        from memorymaster.llm_provider import call_llm, parse_json_response

        # Read last assistant messages
        messages = []
        if transcript_path and os.path.exists(transcript_path):
            lines = Path(transcript_path).read_text(encoding="utf-8", errors="replace").splitlines()
            for line in reversed(lines[-100:]):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Claude Code transcripts wrap role+content inside `message`.
                # Fall back to top-level for older/alternate schemas.
                msg = entry.get("message") if isinstance(entry.get("message"), dict) else entry
                if msg.get("role") != "assistant":
                    continue
                content = msg.get("content", "")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
                if text and len(text) > 30:
                    messages.append(text[:500])
                    if sum(len(m) for m in messages) > 3000:
                        break

        if not messages or sum(len(m) for m in messages) < 50:
            return

        assistant_text = "\n---\n".join(reversed(messages))

        prompt = """You are a memory curator. Extract max 3 non-obvious learnings.
Return JSON array: [{"text": "one-line", "claim_type": "fact|decision|constraint", "subject": "entity", "predicate": "aspect"}]
Only: bug root causes, decisions, gotchas, constraints. Never: credentials, IPs, paths, code. Empty array if nothing worth remembering."""

        response = call_llm(prompt, assistant_text)
        claims = parse_json_response(response)
        claims = [c for c in claims if not SENSITIVE.search(c.get("text", ""))][:3]

        if not claims or not os.path.exists(DB_PATH):
            return

        scope = "project:" + os.path.basename(cwd).lower().replace(" ", "-") if cwd else "global"
        now = datetime.now(timezone.utc).isoformat()

        conn = sqlite3.connect(DB_PATH)
        for c in claims:
            text = c.get("text", "")
            if not text or len(text) < 10:
                continue
            # Duplicate check by content hash
            text_hash = hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]
            idem = f"llm-stop-{text_hash}"
            if conn.execute("SELECT id FROM claims WHERE idempotency_key = ?", (idem,)).fetchone():
                continue
            cur = conn.execute(
                """INSERT INTO claims (text, idempotency_key, normalized_text, claim_type,
                   subject, predicate, scope, status, confidence,
                   source_agent, created_at, updated_at, tier, version, visibility,
                   valid_from)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'candidate', 0.6,
                   'llm-stop-hook', ?, ?, 'working', 1, 'public', ?)""",
                (text, idem, text.lower(), c.get("claim_type", "fact"),
                 c.get("subject", "codebase"), c.get("predicate", "observation"),
                 scope, now, now, now),
            )
            # Auto-citation: steward's min_citations>=1 gate requires at least one
            # row per claim. Without this, every llm-stop-hook claim is born
            # unpromotable. Source is the hook itself; locator = scope for
            # traceability; excerpt preserves the first 200 chars of the claim.
            conn.execute(
                """INSERT INTO citations (claim_id, source, locator, excerpt, created_at)
                   VALUES (?, 'llm-stop-hook', ?, ?, ?)""",
                (cur.lastrowid, scope, text[:200], now),
            )
        conn.commit()
        conn.close()

        provider = os.environ.get("MEMORYMASTER_LLM_PROVIDER", "google")
        sys.stderr.write(f"[MemoryMaster] {provider} extracted {len(claims)} learnings\n")
    except Exception:
        pass


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}

    session_id = data.get("session_id", "unknown")
    # Sanitize session_id
    session_id = re.sub(r"[^a-zA-Z0-9_-]", "", session_id) or "unknown"
    transcript_path = data.get("transcript_path", "")
    cwd = data.get("cwd", os.getcwd())
    stop_hook_active = data.get("stop_hook_active", False)

    # If already in a save cycle, let through (prevents infinite loop)
    if stop_hook_active in (True, "True", "true"):
        sys.stdout.write(json.dumps({"decision": "approve"}))
        return

    # Count human messages
    exchange_count = _count_human_messages(transcript_path)
    last_save = _get_last_save(session_id)
    since_last = exchange_count - last_save

    # Log
    with open(os.path.join(STATE_DIR, "hook.log"), "a") as f:
        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {session_id}: {exchange_count} exchanges, {since_last} since last save\n")

    # Time to block and force save?
    if since_last >= SAVE_INTERVAL and exchange_count > 0:
        _set_last_save(session_id, exchange_count)

        with open(os.path.join(STATE_DIR, "hook.log"), "a") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] BLOCKING at exchange {exchange_count}\n")

        sys.stdout.write(json.dumps({
            "decision": "block",
            "reason": "AUTO-SAVE checkpoint (every 15 messages). Save key learnings from this session to MemoryMaster using mcp__memorymaster__ingest_claim. Ingest: decisions made, bug root causes, gotchas, constraints. Set source_agent to 'claude-session'. NEVER ingest credentials, IPs, tokens, or code. After saving, continue normally."
        }))
        return

    # Store verbatim on every stop (raw conversation storage)
    try:
        from memorymaster.verbatim_store import store_transcript
        if transcript_path and os.path.exists(transcript_path):
            scope = "project:" + os.path.basename(cwd).lower().replace(" ", "-") if cwd else "global"
            store_transcript(DB_PATH, transcript_path, scope=scope, source_agent="stop-hook")
    except Exception:
        pass

    # Not time to block — run passive Gemini extraction
    _run_gemini_extraction(transcript_path, cwd)

    sys.stdout.write(json.dumps({"decision": "approve"}))


if __name__ == "__main__":
    main()
