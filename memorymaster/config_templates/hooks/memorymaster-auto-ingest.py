"""Stop hook: use configurable LLM to extract learnings from transcript and ingest as claims."""
import json, os, sys, sqlite3, re
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = "__MEMORYMASTER_PROJECT_ROOT__"
DB_PATH = os.path.join(PROJECT_ROOT, "memorymaster.db")

sys.path.insert(0, PROJECT_ROOT)

SENSITIVE = re.compile(
    r"(?i)password\s*(?:is|=|:)\s*\S+|secret[:=]\s*\S{8,}|token[:=]\s*\S{20,}"
    r"|sk-[A-Za-z0-9\-]{20,}|ghp_[A-Za-z0-9]{20,}|192\.168\.\d"
    r"|\d{8,}:[A-Za-z0-9_-]{30,}|ubuntu@|ssh\s+\w+@"
)

SYSTEM_PROMPT = """You are a memory curator for an AI coding agent. You receive the last few assistant messages from a coding session.

Extract ONLY non-obvious learnings that would help future sessions. Return a JSON array of claims (max 3). Each claim:
{"text": "concise one-line fact/decision", "claim_type": "fact|decision|constraint", "subject": "entity", "predicate": "aspect"}

Rules:
- Only extract: bug root causes, architectural decisions, gotchas, constraints, integration patterns
- NEVER extract: credentials, IPs, tokens, file paths, code snippets, routine actions
- If nothing worth remembering, return empty array: []
- Keep text under 120 chars, factual, no opinions

Return ONLY valid JSON array, no markdown, no explanation."""


def get_recent_assistant_messages(transcript_path, max_chars=3000):
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8", errors="replace").splitlines()
        messages = []
        for line in reversed(lines[-100:]):
            if not line.strip():
                continue
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
                text = " ".join(
                    c.get("text", "") for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                )
            if text and len(text) > 30:
                messages.append(text[:500])
                if sum(len(m) for m in messages) > max_chars:
                    break
        return "\n---\n".join(reversed(messages))
    except Exception:
        return ""


def ingest(claims, cwd):
    if not claims or not os.path.exists(DB_PATH):
        return 0
    scope = "project:" + os.path.basename(cwd).lower().replace(" ", "-") if cwd else "global"
    now = datetime.now(timezone.utc).isoformat()
    ingested = 0
    try:
        conn = sqlite3.connect(DB_PATH)
        for c in claims:
            text = c.get("text", "")
            if not text or len(text) < 10 or SENSITIVE.search(text):
                continue
            idem = "llm-stop-" + str(hash(text) & 0xFFFFFFFF)
            if conn.execute("SELECT id FROM claims WHERE idempotency_key = ?", (idem,)).fetchone():
                continue
            conn.execute(
                """INSERT INTO claims (text, idempotency_key, normalized_text, claim_type,
                   subject, predicate, scope, status, confidence,
                   source_agent, created_at, updated_at, tier, version, visibility)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'candidate', 0.6,
                   'llm-stop-hook', ?, ?, 'working', 1, 'public')""",
                (text, idem, text.lower(), c.get("claim_type", "fact"),
                 c.get("subject", "codebase"), c.get("predicate", "observation"),
                 scope, now, now),
            )
            ingested += 1
        conn.commit()
        conn.close()
    except Exception:
        pass
    return ingested


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}

    transcript_path = data.get("transcript_path", "")
    cwd = data.get("cwd", os.getcwd())

    assistant_text = get_recent_assistant_messages(transcript_path)
    if not assistant_text or len(assistant_text) < 50:
        sys.stdout.write(json.dumps({"decision": "approve"}))
        return

    from memorymaster.llm_provider import call_llm, parse_json_response
    response = call_llm(SYSTEM_PROMPT, assistant_text)
    claims = parse_json_response(response)
    claims = [c for c in claims if not SENSITIVE.search(c.get("text", ""))][:3]

    if claims:
        count = ingest(claims, cwd)
        if count:
            provider = os.environ.get("MEMORYMASTER_LLM_PROVIDER", "google")
            sys.stderr.write(f"[MemoryMaster] {provider} extracted {count} learnings\n")

    sys.stdout.write(json.dumps({"decision": "approve"}))


if __name__ == "__main__":
    main()
