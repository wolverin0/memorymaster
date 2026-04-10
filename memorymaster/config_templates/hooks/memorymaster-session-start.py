#!/usr/bin/env python3
"""Inject MemoryMaster context at session startup.

Fires on SessionStart (startup|resume). Pulls from memorymaster.db:
  1. Last 5 claims in current project scope (by created_at)
  2. Last steward cycle summary (ingest/decay/supersession counts from events)
  3. Top 3 most-recently-updated wiki articles (filename + description)
  4. Candidate claims older than 24h still awaiting promotion

Returns hookSpecificOutput.additionalContext so Claude starts every session
with MemoryMaster state instead of a blank slate. Graceful degradation: if
DB missing or any query fails, the hook exits silently.

Scope is auto-derived from CWD: the project dir name becomes the scope
suffix (e.g., cwd=memorymaster → scope=project:memorymaster).
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


MM_ROOT = Path(r"__MEMORYMASTER_PROJECT_ROOT__")
DB_PATH = MM_ROOT / "memorymaster.db"
WIKI_ROOT = MM_ROOT / "obsidian-vault" / "wiki"
MAX_RECENT_CLAIMS = 5
MAX_RECENT_ARTICLES = 3
CANDIDATE_REVIEW_HOURS = 24


def _derive_scope() -> str:
    """Derive scope from current working directory."""
    try:
        cwd = Path(os.getcwd()).resolve()
        # If cwd is inside the memorymaster project, scope to it
        if str(cwd).lower().startswith(str(MM_ROOT).lower()):
            return "project:memorymaster"
        # Otherwise: use dir name
        return f"project:{cwd.name.lower()}"
    except Exception:
        return "project"


def _query_recent_claims(conn: sqlite3.Connection, scope: str) -> list[dict]:
    try:
        cur = conn.execute(
            """
            SELECT id, text, claim_type, status, created_at
            FROM claims
            WHERE scope = ? AND status != 'archived'
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (scope, MAX_RECENT_CLAIMS),
        )
        return [
            {
                "id": r[0],
                "text": (r[1] or "")[:180],
                "type": r[2] or "fact",
                "status": r[3],
                "created_at": r[4],
            }
            for r in cur.fetchall()
        ]
    except sqlite3.Error:
        return []


def _query_last_cycle_summary(conn: sqlite3.Connection) -> dict:
    """Count events by type in the last 24h (proxy for last cycle)."""
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        cur = conn.execute(
            """
            SELECT event_type, COUNT(*) FROM events
            WHERE created_at >= ?
            GROUP BY event_type
            """,
            (since,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}
    except sqlite3.Error:
        return {}


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


def _load_recent_wiki_articles(scope: str) -> list[dict]:
    """Find most recent wiki articles for the scope, with description."""
    if not WIKI_ROOT.exists():
        return []
    scope_dir = scope.replace(":", "-")
    candidate_dirs = [
        WIKI_ROOT / scope_dir,
        WIKI_ROOT / "project-memorymaster",
        WIKI_ROOT / "global",
    ]
    articles: list[tuple[float, Path]] = []
    seen: set[Path] = set()
    for d in candidate_dirs:
        if not d.exists():
            continue
        for md in d.glob("*.md"):
            if md.name == "_index.md" or md in seen:
                continue
            seen.add(md)
            try:
                articles.append((md.stat().st_mtime, md))
            except OSError:
                continue
    articles.sort(key=lambda x: x[0], reverse=True)
    out = []
    for _, md in articles[:MAX_RECENT_ARTICLES]:
        desc = _extract_description(md)
        out.append({"name": md.stem, "description": desc})
    return out


def _extract_description(md: Path) -> str:
    try:
        content = md.read_text(encoding="utf-8", errors="replace")[:2000]
    except OSError:
        return ""
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].splitlines():
                if line.strip().startswith("description:"):
                    return line.partition(":")[2].strip().strip('"\'')[:180]
            # Fallback: first non-empty body line
            for line in parts[2].splitlines():
                t = line.strip()
                if t and not t.startswith("#"):
                    return t[:180]
    return ""


def _format_context(
    scope: str,
    claims: list[dict],
    cycle: dict,
    candidates: int,
    articles: list[dict],
) -> str:
    lines = [f"[MemoryMaster session context — scope: {scope}]"]

    if cycle:
        ing = cycle.get("ingest", 0)
        decay = cycle.get("decay", 0)
        sup = cycle.get("supersession", 0)
        validator = cycle.get("validator", 0) + cycle.get(
            "deterministic_validator", 0
        )
        lines.append(
            f"Last 24h activity: {ing} ingested, {validator} validated, "
            f"{sup} superseded, {decay} decayed"
        )

    if candidates:
        lines.append(
            f"WARNING: {candidates} candidate claim(s) older than "
            f"{CANDIDATE_REVIEW_HOURS}h awaiting steward review "
            "(run `mcp__memorymaster__run_cycle` or ignore)"
        )

    if claims:
        lines.append("\nRecent claims in this scope:")
        for c in claims:
            lines.append(
                f"  - #{c['id']} [{c['type']}/{c['status']}] "
                f"{c['text'][:150]}"
            )

    if articles:
        lines.append("\nMost recently updated wiki articles:")
        for a in articles:
            if a["description"]:
                lines.append(f"  - [[{a['name']}]] — {a['description'][:130]}")
            else:
                lines.append(f"  - [[{a['name']}]]")

    lines.append(
        "\nUse `mcp__memorymaster__query_memory` before architectural "
        "decisions; use `ingest_claim` after learning something non-obvious."
    )
    return "\n".join(lines)


def main():
    # Read stdin (may be empty for SessionStart)
    try:
        sys.stdin.read()
    except Exception:
        pass

    if not DB_PATH.exists():
        sys.exit(0)

    try:
        scope = _derive_scope()
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2.0)
        try:
            claims = _query_recent_claims(conn, scope)
            cycle = _query_last_cycle_summary(conn)
            candidates = _query_candidate_claims(conn, scope)
        finally:
            conn.close()
        articles = _load_recent_wiki_articles(scope)
    except Exception:
        sys.exit(0)

    # Skip if there's truly nothing to say
    if not claims and not cycle and not candidates and not articles:
        sys.exit(0)

    context = _format_context(scope, claims, cycle, candidates, articles)
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
