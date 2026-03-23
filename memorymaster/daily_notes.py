"""Daily Notes — automatic session summarization and ghost note detection.

Inspired by Obsidian's Daily Notes pattern:
1. After each session, generate a daily note summarizing what was worked on
2. Track "ghost notes" — topics that keep appearing but haven't been explored
3. When a ghost note gets 3+ references, flag it as important

This turns memorymaster from a flat claim store into a "second brain"
that surfaces recurring themes and unfinished thoughts.

Usage:
    memorymaster daily-note                    # Generate today's daily note
    memorymaster daily-note --date 2026-03-22  # Generate for specific date
    memorymaster ghost-notes                   # Show recurring unresolved topics
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def generate_daily_note(db_path: str, date: str | None = None) -> dict:
    """Generate a daily note from today's feedback and claim activity.

    Summarizes: what was queried, what was ingested, what topics recurred.
    Returns dict with the note content and metadata.
    """
    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        # What was queried today?
        queries = []
        try:
            rows = conn.execute(
                "SELECT query_text, COUNT(*) as cnt FROM usage_feedback WHERE timestamp LIKE ? GROUP BY query_text ORDER BY cnt DESC LIMIT 10",
                (f"{date}%",),
            ).fetchall()
            queries = [(r["query_text"], r["cnt"]) for r in rows]
        except sqlite3.OperationalError:
            pass

        # What was ingested today?
        ingested = []
        try:
            rows = conn.execute(
                "SELECT id, text, claim_type, scope FROM claims WHERE created_at LIKE ? ORDER BY id DESC LIMIT 15",
                (f"{date}%",),
            ).fetchall()
            ingested = [{"id": r["id"], "text": r["text"][:100], "type": r["claim_type"], "scope": r["scope"]} for r in rows]
        except sqlite3.OperationalError:
            pass

        # What claims were accessed most today?
        accessed = []
        try:
            rows = conn.execute(
                "SELECT claim_id, COUNT(*) as cnt FROM usage_feedback WHERE timestamp LIKE ? GROUP BY claim_id ORDER BY cnt DESC LIMIT 10",
                (f"{date}%",),
            ).fetchall()
            for r in rows:
                claim = conn.execute("SELECT text FROM claims WHERE id = ?", (r["claim_id"],)).fetchone()
                if claim:
                    accessed.append({"id": r["claim_id"], "text": claim["text"][:80], "access_count": r["cnt"]})
        except sqlite3.OperationalError:
            pass

        # Extract topics (most common words in queries)
        all_query_text = " ".join(q for q, _ in queries)
        words = [w.lower() for w in all_query_text.split() if len(w) > 3]
        topic_counts = Counter(words)
        topics = [w for w, c in topic_counts.most_common(5) if c > 1]

    finally:
        conn.close()

    # Build the daily note
    note_lines = [
        f"# Daily Note — {date}",
        "",
        f"## Queries ({len(queries)} unique)",
    ]
    for q, cnt in queries[:5]:
        note_lines.append(f"- [{cnt}x] {q[:80]}")

    note_lines.extend(["", f"## New Claims ({len(ingested)})"])
    for item in ingested[:5]:
        note_lines.append(f"- [{item['type'] or 'fact'}] {item['text']}")

    note_lines.extend(["", f"## Most Accessed ({len(accessed)})"])
    for item in accessed[:5]:
        note_lines.append(f"- [{item['access_count']}x] {item['text']}")

    if topics:
        note_lines.extend(["", "## Recurring Topics"])
        for t in topics:
            note_lines.append(f"- [[{t}]]")

    note_content = "\n".join(note_lines)

    return {
        "date": date,
        "queries": len(queries),
        "ingested": len(ingested),
        "accessed": len(accessed),
        "topics": topics,
        "note": note_content,
    }


def find_ghost_notes(db_path: str, min_references: int = 3) -> list[dict]:
    """Find "ghost notes" — topics that keep appearing across queries but
    haven't been fully explored.

    A ghost note is a keyword/topic that appears in 3+ different queries
    but has fewer than 2 confirmed claims about it. These are the ideas
    worth fleshing out.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        # Get all query texts from feedback
        try:
            rows = conn.execute("SELECT DISTINCT query_text FROM usage_feedback").fetchall()
        except sqlite3.OperationalError:
            return []

        # Count word frequency across queries
        word_freq: Counter = Counter()
        for r in rows:
            words = set(r["query_text"].lower().split())
            meaningful = [w for w in words if len(w) > 4]
            word_freq.update(meaningful)

        # Find words that appear in many queries
        ghost_notes = []
        for word, freq in word_freq.most_common(20):
            if freq < min_references:
                continue

            # Check how many confirmed claims contain this word
            try:
                claim_count = conn.execute(
                    "SELECT COUNT(*) as c FROM claims WHERE status = 'confirmed' AND LOWER(text) LIKE ?",
                    (f"%{word}%",),
                ).fetchone()["c"]
            except sqlite3.OperationalError:
                claim_count = 0

            # Ghost note: frequently queried but few claims about it
            if claim_count < 5:
                ghost_notes.append({
                    "topic": word,
                    "query_references": freq,
                    "existing_claims": claim_count,
                    "gap": freq - claim_count,
                    "status": "ghost" if claim_count == 0 else "underdeveloped",
                })

        return sorted(ghost_notes, key=lambda x: -x["gap"])

    finally:
        conn.close()


def export_daily_note_md(db_path: str, output_dir: str, date: str | None = None) -> str:
    """Generate and save a daily note as a .md file."""
    result = generate_daily_note(db_path, date)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    filename = f"{result['date']}.md"
    filepath = output / filename
    filepath.write_text(result["note"], encoding="utf-8")

    return str(filepath)
