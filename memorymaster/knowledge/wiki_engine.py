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

from memorymaster.stores._storage_shared import connect_ro, open_conn

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
    conn = connect_ro(db_path)
    query = """SELECT id, text, claim_type, subject, predicate, object_value,
               scope, confidence, status, human_id, created_at, updated_at, event_time
               FROM claims WHERE status IN ('confirmed', 'candidate')"""
    params: list = []
    if scope_filter:
        query += " AND scope LIKE ?"
        params.append(f"{scope_filter}%")
    query += """ ORDER BY
               COALESCE(subject, 'general') COLLATE NOCASE ASC,
               confidence DESC,
               COALESCE(updated_at, created_at, event_time, '') DESC,
               id ASC"""
    rows = conn.execute(query, params).fetchall()
    conn.close()

    by_subject: dict[str, list[dict]] = {}
    for r in rows:
        subj = r["subject"] or "general"
        by_subject.setdefault(subj, []).append(dict(r))
    return by_subject


def _parse_claim_datetime(value: Any) -> datetime | None:
    from datetime import datetime as datetime_cls

    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime_cls.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        match = re.match(r"^(\d{4}-\d{2}-\d{2})", raw)
        if not match:
            return None
        try:
            parsed = datetime_cls.fromisoformat(match.group(1))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _claim_datetime(claim: dict) -> datetime | None:
    for key in ("event_time", "updated_at", "created_at", "valid_from"):
        parsed = _parse_claim_datetime(claim.get(key))
        if parsed is not None:
            return parsed
    return None


def _latest_claim_datetime(claims: list[dict]) -> datetime | None:
    dates = [_claim_datetime(c) for c in claims]
    valid_dates = [d for d in dates if d is not None]
    return max(valid_dates) if valid_dates else None


def _claim_sort_key(claim: dict) -> tuple[float, float, int]:
    try:
        confidence = float(claim.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    parsed = _claim_datetime(claim)
    timestamp = parsed.timestamp() if parsed is not None else 0.0
    return (-confidence, -timestamp, int(claim.get("id") or 0))


def _claim_timeline_date(claim: dict) -> str:
    parsed = _claim_datetime(claim)
    return parsed.strftime("%Y-%m-%d") if parsed is not None else "undated"


def _claim_set_date(claims: list[dict]) -> str:
    parsed = _latest_claim_datetime(claims)
    return parsed.strftime("%Y-%m-%d") if parsed is not None else "1970-01-01"


def _claim_set_generated_at(articles: list[dict]) -> str:
    dates = [_parse_claim_datetime(a.get("generated_at")) for a in articles]
    valid_dates = [d for d in dates if d is not None]
    if not valid_dates:
        return "undated"
    return max(valid_dates).strftime("%Y-%m-%dT%H:%M:%SZ")


def _call_llm(prompt: str, text: str) -> str:
    from memorymaster.govern import llm_budget

    try:
        from memorymaster.core.llm_provider import call_llm
        return call_llm(prompt, text)
    except llm_budget.LLMBudgetExceeded:
        # Budget abort MUST propagate so absorb() emits its documented
        # `aborted` metadata instead of silently returning empty bodies that
        # skip the remaining subjects while the run reports success.
        raise
    except Exception:
        return ""


def _extract_description(body: str, max_chars: int = 180) -> str:
    """Extract a ~150-char description from article body.

    Walks paragraphs until it finds something substantial. Used for
    progressive disclosure in Bases views and the SessionStart hook.
    """
    if not body:
        return ""
    # Strip markdown artefacts for the description
    for para in body.split("\n\n"):
        clean = para.strip()
        if not clean:
            continue
        # Skip headers, separators, lists
        if clean.startswith(("#", "---", "- ", "* ", "| ", "```")):
            continue
        # Remove wikilinks and emphasis
        clean = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", clean)
        clean = re.sub(r"[*_`]+", "", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        if len(clean) >= 40:
            if len(clean) <= max_chars:
                return clean
            # Cut at last sentence boundary within limit
            cut = clean[:max_chars]
            for sep in (". ", "; ", ", "):
                idx = cut.rfind(sep)
                if idx >= 60:
                    return cut[: idx + 1].strip()
            return cut.rsplit(" ", 1)[0] + "..."
    return ""


def _build_tags(article_type: str, scope: str, claim_types: list[str]) -> list[str]:
    """Derive frontmatter tags from article type + scope + claim distribution."""
    tags: list[str] = []
    tags.append(article_type)
    # Scope tag — e.g., project:memorymaster -> project-memorymaster
    tags.append(re.sub(r"[^a-z0-9-]+", "-", scope.lower()).strip("-"))
    # Unique extra types that differ from majority
    for ct in claim_types:
        t = (ct or "").strip().lower()
        if t and t not in tags:
            tags.append(t)
    # Cap to avoid runaway
    return tags[:8]


def _yaml_escape(value: str) -> str:
    """Quote YAML scalar if it contains special characters."""
    if value is None:
        return '""'
    v = str(value).replace("\r", " ").replace("\n", " ").strip()
    if not v:
        return '""'
    if any(c in v for c in ':#"\''):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return v


_EXPLORED_RE = re.compile(r"^explored\s*:\s*(true|false|yes|no|1|0)\s*$", re.IGNORECASE | re.MULTILINE)


def _read_existing_explored(filepath: Path) -> bool | None:
    """Parse the ``explored`` frontmatter flag from an existing article.

    Returns the bool if the field is present, ``None`` otherwise. Used to
    preserve an operator's "explored: true" decision across re-absorbs —
    same pattern as the sensitivity re-upsert preservation (claim mm-3e07).
    """
    if not filepath.exists():
        return None
    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    fm = parts[1]
    match = _EXPLORED_RE.search(fm)
    if not match:
        return None
    val = match.group(1).strip().lower()
    return val in ("true", "yes", "1")


def _write_article(wiki_dir: Path, scope_dir: str, slug: str, title: str,
                    body: str, article_type: str, scope: str,
                    claim_ids: list[int], related: list[str],
                    *, description: str = "",
                    claim_types: list[str] | None = None,
                    date: str | None = None) -> Path:
    """Write a wiki article with frontmatter.

    Frontmatter schema (obsidian-mind progressive-disclosure pattern):
      - title, type, scope, claims, created, last_updated, date, related
      - description (~150 char summary for Bases + SessionStart hook)
      - tags (for Obsidian graph + Bases filters)
      - explored (true|false): operator-set human-review marker. Defaults
        to false on new articles; PRESERVED on re-absorb if the existing
        article has explored: true (operators reviewed and approved).
        Pattern borrowed from shannhk/llm-wiki — see claim mm-09b4.
    """
    dest = wiki_dir / scope_dir
    dest.mkdir(parents=True, exist_ok=True)

    article_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not description:
        description = _extract_description(body)

    tags = _build_tags(article_type, scope, claim_types or [])

    filepath = dest / f"{slug}.md"
    existing_explored = _read_existing_explored(filepath)
    explored = existing_explored if existing_explored is not None else False

    lines = ["---"]
    lines.append(f"title: {_yaml_escape(title)}")
    if description:
        lines.append(f"description: {_yaml_escape(description)}")
    lines.append(f"type: {article_type}")
    lines.append(f"scope: {scope}")
    lines.append(f"tags: {json.dumps(tags)}")
    lines.append(f"claims: {claim_ids[:20]}")
    lines.append(f"created: {article_date}")
    lines.append(f"last_updated: {article_date}")
    lines.append(f"date: {article_date}")
    lines.append(f"explored: {str(explored).lower()}")
    if related:
        lines.append(f"related: {json.dumps(related[:10])}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title}")
    lines.append("")
    lines.append(body)

    filepath.write_text("\n".join(lines), encoding="utf-8")
    return filepath


def _build_contradiction_callout(claims: list[dict]) -> str:
    """Detect (subject, predicate) contradictions within a single article's
    claim cluster and render them as Obsidian ``> [!contradiction]`` callouts
    that surface inline at read time.

    Empty string if no contradictions. The detector is shared with
    ``vault_linter._detect_contradictions`` so wiki-absorb and lint-vault
    agree on what counts as a contradiction. Pattern from shannhk/llm-wiki —
    see claim mm-09b4.
    """
    if not claims or len(claims) < 2:
        return ""
    try:
        from memorymaster.knowledge.vault_linter import _detect_contradictions
    except ImportError:
        return ""
    contradictions = _detect_contradictions(claims)
    if not contradictions:
        return ""

    lines: list[str] = []
    for c in contradictions:
        # key is "subject|predicate"; render predicate alone since the
        # whole article is about the subject.
        predicate = c["key"].split("|", 1)[-1] or c["key"]
        lines.append(f"> [!contradiction] Conflicting claims on `{predicate}`")
        lines.append("> ")
        lines.append(
            f"> {len(c['claims'])} claims disagree on this. "
            "Higher-confidence first; review and supersede or mark conflicted."
        )
        lines.append("> ")
        for cl in c["claims"][:6]:
            hid = cl.get("human_id") or f"#{cl['id']}"
            val = (cl.get("value") or "").replace("\n", " ").strip()[:120]
            conf = cl.get("confidence")
            conf_str = f"conf={conf:.2f}" if isinstance(conf, (int, float)) else ""
            lines.append(f"> - **{hid}** ({conf_str}): {val}")
        lines.append("")
    return "\n".join(lines)


def _stamp_wiki_binding(db_path: str, claim_ids: list[int], slug: str) -> None:
    """Record which wiki article absorbed each claim (v3.4 bidirectional binding).

    Sets claims.wiki_article so the recall hook can surface "→ [[<slug>]]"
    alongside the claim text. Silent on missing column (pre-3.4 DB).
    """
    if not claim_ids or not slug:
        return
    try:
        # open_conn's WAL + busy_timeout mean a concurrent writer (steward
        # cycle, MCP ingest) doesn't give us an immediate SQLITE_BUSY that
        # silently drops the wiki_article binding.
        conn = open_conn(db_path)
        placeholders = ",".join("?" * len(claim_ids))
        conn.execute(
            f"UPDATE claims SET wiki_article = ? WHERE id IN ({placeholders})",
            (slug, *claim_ids),
        )
        conn.commit()
        conn.close()
    except sqlite3.OperationalError as exc:
        # Column missing on older DB — skip silently; backfill will cover it.
        if "no such column" in str(exc).lower():
            return
        # SQLITE_BUSY / locked or any other operational failure dropped the
        # binding — surface it rather than swallowing it.
        logger.warning("wiki binding stamp failed: %s", exc)
    except Exception as exc:
        logger.warning("wiki binding stamp failed: %s", exc)


def absorb(
    db_path: str,
    wiki_dir: str | Path,
    *,
    scope_filter: str | None = None,
) -> dict[str, Any]:
    """Absorb claims into wiki articles using LLM.

    Honours per-cycle LLM budget caps from ``llm_budget`` — when a cap is
    hit mid-absorption, partial results are returned with ``aborted``
    metadata so callers can see *why* the run stopped instead of silent
    overspend. If a parent scope is already active (e.g. wiki-absorb fired
    from inside ``service.run_cycle`` in the future), defers to it.
    """
    from memorymaster.govern import llm_budget

    if llm_budget.get_current() is not None:
        return _absorb_impl(db_path, wiki_dir, scope_filter=scope_filter)

    with llm_budget.cycle_scope() as budget:
        try:
            result = _absorb_impl(db_path, wiki_dir, scope_filter=scope_filter)
        except llm_budget.LLMBudgetExceeded as exc:
            result = {
                "subjects": 0,
                "articles_written": 0,
                "articles_updated": 0,
                "aborted": True,
                "aborted_reason": exc.reason,
                "aborted_provider": exc.provider,
            }
            logger.warning(
                "wiki_absorb aborted by llm budget: reason=%s provider=%s",
                exc.reason,
                exc.provider,
            )
        result["budget"] = budget.snapshot()
        return result


def _absorb_impl(
    db_path: str,
    wiki_dir: str | Path,
    *,
    scope_filter: str | None = None,
) -> dict[str, Any]:
    """Original wiki absorption implementation, called inside a budget scope."""
    wiki = Path(wiki_dir)
    wiki.mkdir(parents=True, exist_ok=True)

    by_subject = _load_claims_by_topic(db_path, scope_filter)
    if not by_subject:
        return {"subjects": 0, "articles_written": 0, "articles_updated": 0}

    articles_written = 0
    articles_updated = 0
    all_articles: list[dict] = []

    for subject in sorted(by_subject, key=lambda value: value.lower()):
        claims = sorted(by_subject[subject], key=_claim_sort_key)
        if len(claims) < 2:
            continue

        scope = claims[0]["scope"]
        scope_dir = _scope_dirname(scope)
        slug = _safe_name(subject)
        existing_path = wiki / scope_dir / f"{slug}.md"
        article_date = _claim_set_date(claims)
        article_generated_at = _latest_claim_datetime(claims)

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
        article_type = (
            sorted(type_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
            if type_counts else "fact"
        )

        # Find related subjects
        related_subjects = set()
        for c in claims:
            text_lower = str(c["text"]).lower()
            for other_subj in sorted(by_subject, key=lambda value: value.lower()):
                if other_subj != subject and other_subj.lower() in text_lower:
                    related_subjects.add(other_subj)

        related_links = [f"[[{_safe_name(r)}]]" for r in sorted(related_subjects)[:5]]
        claim_ids = [c["id"] for c in claims]

        if existing_path.exists():
            # Update: preserve timeline, rewrite compiled truth
            existing_body = existing_path.read_text(encoding="utf-8", errors="replace")
            # Strip YAML frontmatter first so the LLM is fed the real PROSE,
            # not the title/tags/claims metadata. Without this, every
            # re-absorb rewrites from frontmatter and discards the compiled
            # truth, so it regresses each pass. Same split as
            # absorb_single_claim (split on the '---' fence, maxsplit=2).
            existing_content = existing_body
            frontmatter_parts = existing_body.split("---", 2)
            if existing_body.startswith("---") and len(frontmatter_parts) >= 3:
                existing_content = frontmatter_parts[2].lstrip()
            # Split at --- separator to extract existing timeline. The first
            # segment is the compiled-truth prose (before the truth/timeline
            # divider); later segments hold the timeline.
            existing_timeline = ""
            body_parts = existing_content.split("\n---\n")
            if len(body_parts) >= 2:
                # Find the timeline section
                for i, part in enumerate(body_parts):
                    if "###" in part and ("undated" in part.lower() or "20" in part[:20]):
                        existing_timeline = "\n---\n".join(body_parts[i:])
                        break

            existing_truth = body_parts[0] if body_parts else existing_content
            update_prompt = f"""Rewrite ONLY the compiled truth section of this wiki article with new claims.
The compiled truth should reflect the CURRENT understanding including the new claims.
Do NOT include the timeline section — I will preserve it separately.

Existing compiled truth:
{existing_truth[:1500]}

New claims to integrate:
{claims_text}

Return ONLY the updated compiled truth (no frontmatter, no title, no timeline)."""
            new_truth = _call_llm(update_prompt, "")
            if new_truth and len(new_truth) > 50:
                # Build new timeline entries from new claims
                new_timeline_entries = []
                for c in claims[:10]:
                    date = _claim_timeline_date(c)
                    summary = str(c["text"])[:100].encode("ascii", errors="replace").decode("ascii")
                    source = c.get("claim_type", "fact")
                    new_timeline_entries.append(f"### {date} | {source}\n{summary}")

                # Combine: new truth + existing timeline + new entries
                timeline_section = existing_timeline or "## Timeline\n"
                if new_timeline_entries:
                    timeline_section += "\n" + "\n\n".join(new_timeline_entries) + "\n"

                contradiction_block = _build_contradiction_callout(claims)
                truth_with_callouts = (
                    f"{contradiction_block}\n{new_truth}" if contradiction_block else new_truth
                )
                full_body = truth_with_callouts + "\n\n---\n\n" + timeline_section
                claim_type_list = [c.get("claim_type") or "fact" for c in claims]
                _write_article(wiki, scope_dir, slug, subject.title(), full_body,
                              article_type, scope, claim_ids, related_links,
                              claim_types=claim_type_list, date=article_date)
                _stamp_wiki_binding(db_path, claim_ids, slug)
                articles_updated += 1
        else:
            # Create new with compiled truth + timeline
            context = f"Subject: {subject}\nScope: {scope}\nClaims ({len(claims)}):\n{claims_text}"
            body = _call_llm(ABSORB_PROMPT, context)
            if body and len(body) > 50:
                contradiction_block = _build_contradiction_callout(claims)
                body_with_callouts = (
                    f"{contradiction_block}\n{body}" if contradiction_block else body
                )
                claim_type_list = [c.get("claim_type") or "fact" for c in claims]
                _write_article(wiki, scope_dir, slug, subject.title(), body_with_callouts,
                              article_type, scope, claim_ids, related_links,
                              claim_types=claim_type_list, date=article_date)
                _stamp_wiki_binding(db_path, claim_ids, slug)
                articles_written += 1

        all_articles.append({
            "subject": subject,
            "slug": slug,
            "scope_dir": scope_dir,
            "claims": len(claims),
            "related": sorted(related_subjects),
            "generated_at": (
                article_generated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                if article_generated_at is not None else article_date
            ),
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


def absorb_single_claim(
    claim_id: int,
    db_path: str | Path | None = None,
    wiki_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Absorb one validated claim into its subject article immediately."""
    import os

    db_target = str(
        db_path
        or os.environ.get("MEMORYMASTER_DEFAULT_DB")
        or os.environ.get("MEMORYMASTER_DB")
        or "memorymaster.db"
    )
    wiki = Path(wiki_dir or os.environ.get("MEMORYMASTER_WIKI_DIR") or "obsidian-vault/wiki")
    wiki.mkdir(parents=True, exist_ok=True)

    conn = connect_ro(db_target)
    try:
        row = conn.execute(
            """SELECT id, text, claim_type, subject, predicate, object_value,
                      scope, confidence, status, human_id
               FROM claims
               WHERE id = ? AND status IN ('confirmed', 'candidate')""",
            (claim_id,),
        ).fetchone()
        if row is None:
            return {"claim_id": claim_id, "absorbed": False, "reason": "not_found_or_inactive"}

        subject = row["subject"] or "general"
        scope = row["scope"] or "default"
        if row["subject"] is None:
            rows = conn.execute(
                """SELECT id, text, claim_type, subject, predicate, object_value,
                          scope, confidence, status, human_id
                   FROM claims
                   WHERE subject IS NULL AND scope = ? AND status IN ('confirmed', 'candidate')
                   ORDER BY confidence DESC""",
                (scope,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, text, claim_type, subject, predicate, object_value,
                          scope, confidence, status, human_id
                   FROM claims
                   WHERE subject = ? AND scope = ? AND status IN ('confirmed', 'candidate')
                   ORDER BY confidence DESC""",
                (row["subject"], scope),
            ).fetchall()
    finally:
        conn.close()

    claims = [dict(r) for r in rows] or [dict(row)]
    scope_dir = _scope_dirname(scope)
    slug = _safe_name(subject)
    existing_path = wiki / scope_dir / f"{slug}.md"
    claim_ids = [int(c["id"]) for c in claims]
    claim_types = [c.get("claim_type") or "fact" for c in claims]
    article_type = max(set(claim_types), key=claim_types.count) if claim_types else "fact"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    claims_text = "\n".join(
        "- [{}] {}: {}".format(
            c.get("claim_type", "fact"),
            c.get("predicate", ""),
            str(c["text"])[:200].encode("ascii", errors="replace").decode("ascii"),
        )
        for c in claims[:30]
    )
    timeline_entries = "\n\n".join(
        "### {} | {}\n{}".format(
            now,
            c.get("claim_type") or "fact",
            str(c["text"])[:100].encode("ascii", errors="replace").decode("ascii"),
        )
        for c in claims
        if int(c["id"]) == claim_id
    )
    was_update = existing_path.exists()

    if was_update:
        existing_body = existing_path.read_text(encoding="utf-8", errors="replace")
        existing_content = existing_body
        frontmatter_parts = existing_body.split("---", 2)
        if existing_body.startswith("---") and len(frontmatter_parts) >= 3:
            existing_content = frontmatter_parts[2].lstrip()
        body_parts = existing_content.split("\n---\n")
        prompt = f"""Rewrite ONLY the compiled truth section of this wiki article with this validated claim.
Do NOT include the timeline section.

Existing article:
{existing_content[:1500]}

Validated claim:
{claims_text}

Return ONLY the updated compiled truth (no frontmatter, no title, no timeline)."""
        truth = _call_llm(prompt, "")
        if not truth or len(truth) <= 50:
            truth = body_parts[0] if body_parts else existing_content
        timeline_section = "## Timeline\n"
        for part in body_parts:
            if "###" in part and ("undated" in part.lower() or "20" in part[:40]):
                timeline_section = part
                break
        if timeline_entries:
            timeline_section += "\n" + timeline_entries + "\n"
        body = truth + "\n\n---\n\n" + timeline_section
    else:
        context = f"Subject: {subject}\nScope: {scope}\nClaims ({len(claims)}):\n{claims_text}"
        body = _call_llm(ABSORB_PROMPT, context)
        if not body or len(body) <= 50:
            body = (
                f"{subject.title()} captures validated MemoryMaster claims for this scope and "
                "keeps the compiled understanding close to the latest validator evidence.\n\n"
                f"## Compiled Truth\n{claims_text}\n\n---\n\n## Timeline\n{timeline_entries}\n"
            )

    contradiction_block = _build_contradiction_callout(claims)
    if contradiction_block:
        body = f"{contradiction_block}\n{body}"
    path = _write_article(
        wiki, scope_dir, slug, subject.title(), body, article_type,
        scope, claim_ids, [], claim_types=claim_types,
    )
    _stamp_wiki_binding(db_target, claim_ids, slug)
    _write_backlinks(wiki)
    return {
        "claim_id": claim_id,
        "absorbed": True,
        "article": str(path),
        "slug": slug,
        "claims": len(claims),
        "updated": was_update,
    }


def cleanup(wiki_dir: str | Path, scope_filter: str | None = None) -> dict[str, Any]:
    """Audit and rewrite weak articles."""
    wiki = Path(wiki_dir)
    audited = 0
    rewritten = 0

    # Sorted so the every-5th audit cadence is deterministic across platforms
    # (rglob yields filesystem order, which differs between NTFS and ext4).
    for md_file in sorted(wiki.rglob("*.md")):
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
    conn = connect_ro(db_path)
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
                if not entity or not desc:
                    continue
                # Create only the LLM-SELECTED entity, not a blanket
                # scope-wide absorb. Resolve a representative claim for this
                # subject and absorb it individually so `created` reflects
                # the entities the LLM actually chose.
                claim_id = _resolve_subject_claim_id(db_path, entity, scope_filter)
                if claim_id is None:
                    continue
                result = absorb_single_claim(claim_id, db_path, wiki_dir)
                if result.get("absorbed") and not result.get("updated"):
                    created += 1
        except (json.JSONDecodeError, KeyError):
            pass

    return {"missing": len(missing), "created": created}


def _resolve_subject_claim_id(
    db_path: str, subject: str, scope_filter: str | None = None
) -> int | None:
    """Return the highest-confidence active claim id for a subject.

    Used by ``breakdown`` to turn an LLM-selected entity into a concrete
    claim that ``absorb_single_claim`` can materialise into its own article.
    """
    try:
        # Read-only; a missing/broken DB keeps the None contract below.
        conn = connect_ro(db_path)
    except sqlite3.OperationalError:
        return None
    try:
        query = (
            "SELECT id FROM claims "
            "WHERE subject = ? AND status IN ('confirmed','candidate')"
        )
        params: list = [subject]
        if scope_filter:
            query += " AND scope LIKE ?"
            params.append(f"{scope_filter}%")
        query += " ORDER BY confidence DESC, id ASC LIMIT 1"
        row = conn.execute(query, params).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return int(row[0]) if row else None


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
        for a in sorted(scope_articles, key=lambda x: (-x["claims"], x["slug"])):
            related_str = ", ".join(f"[[{_safe_name(r)}]]" for r in a["related"][:3])
            lines.append(f"- [[{a['slug']}|{a['subject']}]] ({a['claims']} claims) {related_str}")
        lines.append("")
        (dest / "_index.md").write_text("\n".join(lines), encoding="utf-8")

    # Master index
    lines = ["# Wiki Master Index", ""]
    lines.append(f"Generated: {_claim_set_generated_at(articles)}")
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

    for md_file in sorted(wiki.rglob("*.md")):
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
    for target in sorted(backlinks):
        seen = set()
        deduped = []
        for entry in sorted(backlinks[target], key=lambda item: (item["from"], item["context"])):
            key = entry["from"]
            if key not in seen:
                seen.add(key)
                deduped.append(entry)
        backlinks[target] = deduped

    (wiki / "_backlinks.json").write_text(
        json.dumps(dict(sorted(backlinks.items())), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _register_lifecycle_autopromote() -> None:
    """Register the wiki autopromote callback into lifecycle.

    P2 phase0 cycle cut: lifecycle (core) must never import wiki_engine
    (knowledge), so the dependency is inverted — wiki_engine (and service
    wiring) registers an adapter into ``lifecycle.on_claim_confirmed``.
    """
    from memorymaster.core import lifecycle as _lifecycle

    def _absorb_on_confirm(claim_id: int, db_path: str | None = None) -> None:
        # Late module-global lookup so monkeypatching
        # wiki_engine.absorb_single_claim is honoured at call time.
        absorb_single_claim(claim_id, db_path=db_path)

    if _lifecycle.on_claim_confirmed is None:
        _lifecycle.on_claim_confirmed = _absorb_on_confirm


_register_lifecycle_autopromote()
