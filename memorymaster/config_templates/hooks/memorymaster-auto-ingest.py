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
import hashlib
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = "__MEMORYMASTER_PROJECT_ROOT__"
DB_PATH = os.path.join(PROJECT_ROOT, "memorymaster.db")
STATE_DIR = os.path.join(os.path.expanduser("~"), ".memorymaster", "hook_state")
SAVE_INTERVAL = 15  # Block every N human messages

sys.path.insert(0, PROJECT_ROOT)

os.makedirs(STATE_DIR, exist_ok=True)

# Sensitivity filtering delegated to memorymaster.security.redact_text — the
# canonical 25+ pattern set. The previous local SENSITIVE regex covered ~6 of
# them and missed bearer/JWT/AWS/Stripe/Slack tokens, home path username
# leaks, and card numbers. Discovered 2026-05-04 by overnight audit (F-2).
# Import is deferred into _is_sensitive_claim() because sys.path.insert above
# runs at module load — keeping the import lazy avoids ImportError at hook
# install time when the package isn't on PYTHONPATH yet.
def _is_sensitive_claim(c: dict) -> bool:
    """Return True if any of (text, subject, predicate, object_value) trips
    the canonical sensitivity filter. Caller drops the claim entirely."""
    try:
        from memorymaster.security import redact_text
    except ImportError:
        # Fail-closed: if the canonical filter can't be imported, refuse the
        # claim. Prevents shipping unreviewed text on a broken install.
        return True
    joined = " | ".join(
        str(c.get(k, "") or "")
        for k in ("text", "subject", "predicate", "object_value")
    )
    _, findings = redact_text(joined)
    return bool(findings)


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
        claims = [c for c in claims if not _is_sensitive_claim(c)][:3]

        if not claims or not os.path.exists(DB_PATH):
            return

        scope = "project:" + os.path.basename(cwd).lower().replace(" ", "-") if cwd else "global"

        # Route through MemoryService.ingest instead of raw SQL so claims gain
        # the canonical ingest path: sensitivity sanitize (defense-in-depth on
        # top of the _is_sensitive_claim drop above), content-hash + idempotency
        # dedup, entity resolution, auto-citation, observability, and webhook.
        # Mirrors _run_rule_extraction, which already uses the service.
        from memorymaster.service import MemoryService
        from memorymaster.models import CitationInput

        svc = MemoryService(DB_PATH, workspace_root=Path(cwd or PROJECT_ROOT))
        ingested = 0
        for c in claims:
            text = c.get("text", "")
            if not text or len(text) < 10:
                continue
            text_hash = hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]
            try:
                svc.ingest(
                    text=text,
                    citations=[CitationInput(source="llm-stop-hook", locator=scope, excerpt=text[:200])],
                    idempotency_key=f"llm-stop-{text_hash}",
                    claim_type=c.get("claim_type", "fact"),
                    subject=c.get("subject", "codebase"),
                    predicate=c.get("predicate", "observation"),
                    scope=scope,
                    confidence=0.6,
                    source_agent="llm-stop-hook",
                )
                ingested += 1
            except Exception:
                continue  # one bad claim must not abort the rest

        provider = os.environ.get("MEMORYMASTER_LLM_PROVIDER", "google")
        sys.stderr.write(f"[MemoryMaster] {provider} extracted {ingested} learnings\n")
    except Exception:
        pass


def _run_rule_extraction(transcript_path, cwd):
    """R1b ongoing: mine the latest correction in this session into a rule claim.

    Reuses memorymaster.rule_miner.mine_transcript_rules (single source of truth
    for the correction->rule prompt + ingest path). Bounded to one window per
    stop to keep the hook fast; rules land as low-confidence candidates."""
    try:
        if not transcript_path or not os.path.exists(transcript_path) or not os.path.exists(DB_PATH):
            return
        from memorymaster.rule_miner import mine_transcript_rules
        from memorymaster.service import MemoryService

        scope = "project:" + os.path.basename(cwd).lower().replace(" ", "-") if cwd else "global"
        svc = MemoryService(DB_PATH, workspace_root=Path(cwd or PROJECT_ROOT))
        stats = mine_transcript_rules(transcript_path, svc, scope=scope, max_windows=1)
        if stats.get("ingested"):
            sys.stderr.write(f"[MemoryMaster] mined {stats['ingested']} rule(s) from corrections\n")
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

    # R1b: mine the latest correction in this session into a rule claim
    _run_rule_extraction(transcript_path, cwd)

    sys.stdout.write(json.dumps({"decision": "approve"}))


if __name__ == "__main__":
    main()
