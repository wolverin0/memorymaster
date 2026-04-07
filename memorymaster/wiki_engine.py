"""Wiki engine — absorb claims into structured articles, cleanup, breakdown.

Implements Karpathy + Farza's approach: articles that have a POINT,
organized by theme not chronology, Wikipedia tone.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SAFE_RE = re.compile(r"[^a-z0-9_-]+")

ABSORB_PROMPT = """You are a technical writer creating a wiki article about a software project.
Given a set of claims about a topic, write a cohesive article with TWO sections:

SECTION 1 — COMPILED TRUTH (above the line):
- The current, always-updated understanding of this topic
- Has a clear POINT — not just "facts about X" but "X works this way because Y"
- Organized by THEME, not chronology
- Wikipedia tone: flat, factual, no "interestingly", no "it should be noted"
- Includes [[wikilinks]] to related topics
- Has sections with ## headers
- Start with a one-paragraph summary

Then write exactly this separator: ---

SECTION 2 — TIMELINE (below the line):
- Append-only chronological evidence entries
- Each entry: ### YYYY-MM-DD | source\\nOne-line summary of what was learned
- Use dates from the claims if available, otherwise use "undated"
- This section is NEVER rewritten, only appended to

Return ONLY the article body (no frontmatter, no title)."""

CLEANUP_PROMPT = """You are a wiki editor. Review this article and rate it 1-10:
- Does it tell a coherent story? (not a chronological dump)
- Organized by theme?
- Would a reader learn something non-obvious?
- Uses Wikipedia tone?

Return JSON: {"score": N, "issues": ["issue1", ...], "rewrite": "full rewritten article if score < 6, else empty string"}
Return ONLY valid JSON."""

BREAKDOWN_PROMPT = """Given these article titles and the list of entities mentioned 3+ times without own articles, pick the top 5 that most deserve their own article. For each, write a 2-sentence description of what the article should cover.

Return JSON array: [{"entity": "name", "description": "what to cover", "mentioned_in": ["article1", "article2"]}]
Return ONLY valid JSON."""


def _safe_name(text: str) -> str:
    return _SAFE_RE.sub("-", text.lower()).strip("-")[:60] or "misc"


def _scope_dirname(scope: str) -> str:
    parts = scope.split(":")
    name = "-".join(parts[:2]) if len(parts) >= 2 else parts[0]
    return _SAFE_RE.sub("-", name.lower()).strip("-") or "default"


def _load_claims_by_topic(db_path: str, scope_filter: str | None = None) -> dict[str, list[dict]]:
    """Load claims grouped by subject."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    query = """SELECT id, text, claim_type, subject, predicate, object_value,
               scope, confidence, status, human_id
               FROM claims WHERE status IN ('confirmed', 'candidate')"""
    params: list = []
    if scope_filter:
        query += " AND scope LIKE ?"
        params.append(f"{scope_filter}%")
    query += " ORDER BY confidence DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    by_subject: dict[str, list[dict]] = {}
    for r in rows:
        subj = r["subject"] or "general"
        by_subject.setdefault(subj, []).append(dict(r))
    return by_subject


def _call_llm(prompt: str, text: str) -> str:
    try:
        from memorymaster.llm_provider import call_llm
        return call_llm(prompt, text)
    except Exception:
        return ""


def _write_article(wiki_dir: Path, scope_dir: str, slug: str, title: str,
                    body: str, article_type: str, scope: str,
                    claim_ids: list[int], related: list[str]) -> Path:
    """Write a wiki article with frontmatter."""
    dest = wiki_dir / scope_dir
    dest.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = ["---"]
    lines.append(f"title: {title}")
    lines.append(f"type: {article_type}")
    lines.append(f"scope: {scope}")
    lines.append(f"claims: {claim_ids[:20]}")
    lines.append(f"created: {now}")
    lines.append(f"last_updated: {now}")
    if related:
        lines.append(f"related: {json.dumps(related[:10])}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title}")
    lines.append("")
    lines.append(body)

    filepath = dest / f"{slug}.md"
    filepath.write_text("\n".join(lines), encoding="utf-8")
    return filepath


def absorb(
    db_path: str,
    wiki_dir: str | Path,
    *,
    scope_filter: str | None = None,
) -> dict[str, Any]:
    """Absorb claims into wiki articles using LLM."""
    wiki = Path(wiki_dir)
    wiki.mkdir(parents=True, exist_ok=True)

    by_subject = _load_claims_by_topic(db_path, scope_filter)
    if not by_subject:
        return {"subjects": 0, "articles_written": 0, "articles_updated": 0}

    articles_written = 0
    articles_updated = 0
    all_articles: list[dict] = []

    for subject, claims in by_subject.items():
        if len(claims) < 2:
            continue

        scope = claims[0]["scope"]
        scope_dir = _scope_dirname(scope)
        slug = _safe_name(subject)
        existing_path = wiki / scope_dir / f"{slug}.md"

        # Prepare claims text for LLM
        claims_text = "\n".join(
            "- [{}] {}: {}".format(
                c.get("claim_type", "fact"),
                c.get("predicate", ""),
                str(c["text"])[:200].encode("ascii", errors="replace").decode("ascii"),
            )
            for c in claims[:30]
        )

        # Determine article type from majority claim_type
        type_counts: dict[str, int] = {}
        for c in claims:
            t = c.get("claim_type") or "fact"
            type_counts[t] = type_counts.get(t, 0) + 1
        article_type = max(type_counts, key=type_counts.get) if type_counts else "fact"

        # Find related subjects
        related_subjects = set()
        for c in claims:
            text_lower = str(c["text"]).lower()
            for other_subj in by_subject:
                if other_subj != subject and other_subj.lower() in text_lower:
                    related_subjects.add(other_subj)

        related_links = [f"[[{_safe_name(r)}]]" for r in list(related_subjects)[:5]]
        claim_ids = [c["id"] for c in claims]

        if existing_path.exists():
            # Update: preserve timeline, rewrite compiled truth
            existing_body = existing_path.read_text(encoding="utf-8", errors="replace")
            # Split at --- separator to extract existing timeline
            existing_timeline = ""
            body_parts = existing_body.split("\n---\n")
            if len(body_parts) >= 2:
                # Everything after frontmatter's --- and the compiled truth's ---
                # Find the timeline section
                for i, part in enumerate(body_parts):
                    if "###" in part and ("undated" in part.lower() or "20" in part[:20]):
                        existing_timeline = "\n---\n".join(body_parts[i:])
                        break

            update_prompt = f"""Rewrite ONLY the compiled truth section of this wiki article with new claims.
The compiled truth should reflect the CURRENT understanding including the new claims.
Do NOT include the timeline section — I will preserve it separately.

Existing compiled truth:
{body_parts[0][:1500] if body_parts else existing_body[:1500]}

New claims to integrate:
{claims_text}

Return ONLY the updated compiled truth (no frontmatter, no title, no timeline)."""
            new_truth = _call_llm(update_prompt, "")
            if new_truth and len(new_truth) > 50:
                # Build new timeline entries from new claims
                now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                new_timeline_entries = []
                for c in claims[:10]:
                    date = now
                    summary = str(c["text"])[:100].encode("ascii", errors="replace").decode("ascii")
                    source = c.get("claim_type", "fact")
                    new_timeline_entries.append(f"### {date} | {source}\n{summary}")

                # Combine: new truth + existing timeline + new entries
                timeline_section = existing_timeline or "## Timeline\n"
                if new_timeline_entries:
                    timeline_section += "\n" + "\n\n".join(new_timeline_entries) + "\n"

                full_body = new_truth + "\n\n---\n\n" + timeline_section
                _write_article(wiki, scope_dir, slug, subject.title(), full_body,
                              article_type, scope, claim_ids, related_links)
                articles_updated += 1
        else:
            # Create new with compiled truth + timeline
            context = f"Subject: {subject}\nScope: {scope}\nClaims ({len(claims)}):\n{claims_text}"
            body = _call_llm(ABSORB_PROMPT, context)
            if body and len(body) > 50:
                _write_article(wiki, scope_dir, slug, subject.title(), body,
                              article_type, scope, claim_ids, related_links)
                articles_written += 1

        all_articles.append({
            "subject": subject,
            "slug": slug,
            "scope_dir": scope_dir,
            "claims": len(claims),
            "related": list(related_subjects),
        })

    # Write scope indexes
    _write_indexes(wiki, all_articles)

    # Write backlinks
    _write_backlinks(wiki)

    return {
        "subjects": len(by_subject),
        "articles_written": articles_written,
        "articles_updated": articles_updated,
        "total_articles": articles_written + articles_updated,
    }


def cleanup(wiki_dir: str | Path, scope_filter: str | None = None) -> dict[str, Any]:
    """Audit and rewrite weak articles."""
    wiki = Path(wiki_dir)
    audited = 0
    rewritten = 0

    for md_file in wiki.rglob("*.md"):
        if md_file.name.startswith("_") or md_file.parent.name == "queries":
            continue
        if scope_filter:
            if scope_filter not in str(md_file):
                continue

        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        if len(content) < 100:
            continue

        audited += 1
        # Only audit every 5th article to save API calls
        if audited % 5 != 0:
            continue

        response = _call_llm(CLEANUP_PROMPT, content[:2000])
        if not response:
            continue

        try:
            # Strip markdown fences
            clean = response.strip()
            if clean.startswith("```"):
                clean = re.sub(r"^```(?:json)?\n?", "", clean)
                clean = re.sub(r"\n?```$", "", clean)
            result = json.loads(clean)
            if result.get("score", 10) < 6 and result.get("rewrite"):
                # Keep frontmatter, replace body
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    new_content = f"---{parts[1]}---\n\n{result['rewrite']}"
                    md_file.write_text(new_content, encoding="utf-8")
                    rewritten += 1
        except (json.JSONDecodeError, KeyError):
            continue

    return {"audited": audited, "rewritten": rewritten}


def breakdown(db_path: str, wiki_dir: str | Path, scope_filter: str | None = None) -> dict[str, Any]:
    """Find missing articles and create them."""
    wiki = Path(wiki_dir)

    # Get existing article subjects
    existing_subjects = set()
    for md_file in wiki.rglob("*.md"):
        if not md_file.name.startswith("_"):
            existing_subjects.add(md_file.stem)

    # Get all subjects from DB
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    query = "SELECT subject, COUNT(*) as cnt FROM claims WHERE status IN ('confirmed','candidate')"
    params: list = []
    if scope_filter:
        query += " AND scope LIKE ?"
        params.append(f"{scope_filter}%")
    query += " GROUP BY subject HAVING cnt >= 3 ORDER BY cnt DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    missing = []
    for r in rows:
        subj = r["subject"] or ""
        if subj and _safe_name(subj) not in existing_subjects:
            missing.append({"entity": subj, "claims": r["cnt"]})

    if not missing:
        return {"missing": 0, "created": 0}

    # Use LLM to pick top 5
    articles_text = ", ".join(sorted(existing_subjects)[:30])
    missing_text = "\n".join(f"- {m['entity']} ({m['claims']} claims)" for m in missing[:20])
    context = f"Existing articles: {articles_text}\n\nMissing entities:\n{missing_text}"
    response = _call_llm(BREAKDOWN_PROMPT, context)

    created = 0
    if response:
        try:
            clean = response.strip()
            if clean.startswith("```"):
                clean = re.sub(r"^```(?:json)?\n?", "", clean)
                clean = re.sub(r"\n?```$", "", clean)
            suggestions = json.loads(clean)
            for s in suggestions[:5]:
                entity = s.get("entity", "")
                desc = s.get("description", "")
                if entity and desc:
                    # Create stub article
                    result = absorb(db_path, wiki_dir, scope_filter=scope_filter)
                    created += result.get("articles_written", 0)
                    break  # absorb handles all at once
        except (json.JSONDecodeError, KeyError):
            pass

    return {"missing": len(missing), "created": created}


def _write_indexes(wiki: Path, articles: list[dict]) -> None:
    """Write _index.md for each scope and a master index."""
    by_scope: dict[str, list[dict]] = {}
    for a in articles:
        by_scope.setdefault(a["scope_dir"], []).append(a)

    for scope_dir, scope_articles in by_scope.items():
        dest = wiki / scope_dir
        dest.mkdir(parents=True, exist_ok=True)
        lines = [f"# {scope_dir}", ""]
        lines.append(f"{len(scope_articles)} articles.")
        lines.append("")
        for a in sorted(scope_articles, key=lambda x: -x["claims"]):
            related_str = ", ".join(f"[[{_safe_name(r)}]]" for r in a["related"][:3])
            lines.append(f"- [[{a['slug']}|{a['subject']}]] ({a['claims']} claims) {related_str}")
        lines.append("")
        (dest / "_index.md").write_text("\n".join(lines), encoding="utf-8")

    # Master index
    lines = ["# Wiki Master Index", ""]
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append(f"Scopes: {len(by_scope)}")
    total = sum(len(a) for a in by_scope.values())
    lines.append(f"Total articles: {total}")
    # Last steward run timestamp
    try:
        import sqlite3 as _sql
        _db = str(wiki.parent / "memorymaster.db")
        if Path(_db).exists():
            _c = _sql.connect(_db)
            _last = _c.execute("SELECT created_at FROM events WHERE event_type='validator' ORDER BY created_at DESC LIMIT 1").fetchone()
            if _last:
                lines.append(f"Last steward run: {_last[0]}")
            _c.close()
    except Exception:
        pass
    lines.append("")
    for scope_dir in sorted(by_scope.keys()):
        count = len(by_scope[scope_dir])
        lines.append(f"- [[{scope_dir}/_index|{scope_dir}]] ({count} articles)")
    lines.append("")
    (wiki / "_index.md").write_text("\n".join(lines), encoding="utf-8")


def _write_backlinks(wiki: Path) -> None:
    """Scan articles for [[wikilinks]] and build reverse index with context."""
    wikilink_re = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
    backlinks: dict[str, list[dict[str, str]]] = {}

    for md_file in wiki.rglob("*.md"):
        if md_file.name.startswith("_"):
            continue
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
            source = md_file.stem
            for line in content.splitlines():
                # Skip frontmatter lines
                if line.strip().startswith(("related:", "claims:", "---")):
                    continue
                for match in wikilink_re.finditer(line):
                    target = match.group(1).split("/")[-1]
                    if target != source:
                        ctx = line.strip()[:150]
                        backlinks.setdefault(target, []).append({
                            "from": source,
                            "context": ctx,
                        })
        except Exception:
            continue

    # Dedupe by (target, from) pair
    for target in backlinks:
        seen = set()
        deduped = []
        for entry in backlinks[target]:
            key = entry["from"]
            if key not in seen:
                seen.add(key)
                deduped.append(entry)
        backlinks[target] = deduped

    (wiki / "_backlinks.json").write_text(
        json.dumps(backlinks, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
