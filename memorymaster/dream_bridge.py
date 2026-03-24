"""Dream Bridge — export/import MemoryMaster claims to/from Claude Code Auto Dream memory format.

Bridges the MemoryMaster claim database into Claude Code's project memory directory,
allowing bidirectional sync between the two memory systems.

Claude Code stores memories in ~/.claude/projects/<project-slug>/memory/ as markdown
files with YAML frontmatter.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sensitivity filter
# ---------------------------------------------------------------------------

_SENSITIVE_PATTERNS = re.compile(
    r"(?i)"
    r"\[REDACTED"
    r"|api[_-]?key\s*[:=]"
    r"|password\s*[:=]"
    r"|secret\s*[:=]"
    r"|token\s*[:=]"
    r"|bearer\s+[A-Za-z0-9\-._~+/]+"
    r"|sk-[A-Za-z0-9]{20,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|AIzaSy[A-Za-z0-9\-_]{30,}"
    # Private/internal IPs (anywhere in text, including URLs)
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    # Personal/user-specific paths
    r"|[A-Z]:\\.*\\(?:OneDrive|Users\\[a-z])"
    r"|/home/\w+"
    # SSH/SCP commands with hosts or passwords
    r"|ssh\s+\w+@"
    r"|sshpass\b"
    r"|scp\s+.*@"
    # Raw credential patterns
    r"|cat\s*>\s*/tmp/.*(?:pw|pass|key|cred)"
    r"|ENDPW"
    r"|esxi_pass"
)

# Claims that are just code snippets or deployment scripts are not useful memories
_NOISE_PATTERNS = re.compile(
    r"(?i)"
    # Bash/shell code blocks dominating the text
    r"^```(?:bash|sh|shell)"
    # Step-by-step deployment instructions (not conceptual knowledge)
    r"|^##\s*(?:Step\s+\d|OR MANUALLY|Build and deploy|Testing Workflow)"
    # Bare URLs without context
    r"|^(?:https?://\S+)$"
    # Session artifacts
    r"|^##\s*SESSION\s*START"
)


def _is_sensitive(text: str) -> bool:
    """Return True if text contains credentials, private IPs, or is just noise."""
    if not text:
        return False
    if _SENSITIVE_PATTERNS.search(text):
        return True
    if _NOISE_PATTERNS.search(text.strip()):
        return True
    # Reject texts that are mostly code (>60% lines start with common code chars)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) >= 3:
        code_lines = sum(1 for l in lines if l.startswith(("$", "#!", "cd ", "pip ", "npm ",
                         "curl ", "docker ", "git ", "scp ", "rsync ", "cat ", "echo ",
                         "sudo ", "chmod ", "mkdir ", "wget ")))
        if code_lines / len(lines) > 0.5:
            return True
    return False


def _is_near_duplicate(text: str, seen_texts: list[str], threshold: float = 0.7) -> bool:
    """Return True if text is too similar to something already exported."""
    if not text or not seen_texts:
        return False
    text_words = set(text.lower().split())
    if not text_words:
        return False
    for seen in seen_texts:
        seen_words = set(seen.lower().split())
        if not seen_words:
            continue
        overlap = len(text_words & seen_words)
        similarity = overlap / max(len(text_words), len(seen_words))
        if similarity >= threshold:
            return True
    return False


# ---------------------------------------------------------------------------
# Slugify
# ---------------------------------------------------------------------------

def _slugify(text: str, max_len: int = 40) -> str:
    """Lowercase, replace non-alphanum with dashes, collapse multiples."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:max_len].rstrip("-")


# ---------------------------------------------------------------------------
# YAML frontmatter helpers (no pyyaml dependency)
# ---------------------------------------------------------------------------

def _dump_frontmatter(meta: dict[str, str]) -> str:
    """Serialize a flat string dict to YAML frontmatter block."""
    lines = ["---"]
    for key, value in meta.items():
        # Escape values that would confuse naive YAML
        safe = str(value).replace('"', '\\"')
        lines.append(f'{key}: "{safe}"')
    lines.append("---")
    return "\n".join(lines)


def _parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """Parse YAML frontmatter from a markdown string.

    Returns (meta_dict, body) where body is everything after the closing ---.
    """
    meta: dict[str, str] = {}
    body = content
    stripped = content.lstrip("\n")
    if not stripped.startswith("---"):
        return meta, body

    parts = stripped.split("---", 2)
    if len(parts) < 3:
        return meta, body

    raw_yaml = parts[1].strip()
    body = parts[2].lstrip("\n")

    for line in raw_yaml.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r'^(\w+)\s*:\s*"?(.*?)"?\s*$', line)
        if match:
            meta[match.group(1)] = match.group(2)

    return meta, body


# ---------------------------------------------------------------------------
# Type mapping
# ---------------------------------------------------------------------------

_FEEDBACK_KEYWORDS = {"preference", "correction", "workflow", "style", "feedback"}
_PROJECT_KEYWORDS = {"fact", "technical", "architecture", "decision", "config", "project"}
_USER_KEYWORDS = {"person", "role", "identity", "user"}
_REFERENCE_KEYWORDS = {"url", "endpoint", "tool", "integration", "reference", "api"}


def claim_to_dream_type(claim: dict) -> str:
    """Map a MemoryMaster claim to Auto Dream type based on category/tags."""
    tags_raw = claim.get("tags") or ""
    category = (claim.get("category") or "").lower()
    claim_type = (claim.get("claim_type") or "").lower()
    subject = (claim.get("subject") or "").lower()

    # Combine all classifiable text
    tokens = set()
    tokens.add(category)
    tokens.add(claim_type)
    tokens.add(subject)
    for tag in re.split(r"[,;\s]+", tags_raw):
        tokens.add(tag.strip().lower())
    tokens.discard("")

    if tokens & _FEEDBACK_KEYWORDS:
        return "feedback"
    if tokens & _USER_KEYWORDS:
        return "user"
    if tokens & _REFERENCE_KEYWORDS:
        return "reference"
    if tokens & _PROJECT_KEYWORDS:
        return "project"
    return "project"


# ---------------------------------------------------------------------------
# Discover memory directory
# ---------------------------------------------------------------------------

def _compute_project_slug(project_path: str) -> str:
    """Compute the Claude Code project slug from a filesystem path.

    Claude Code slugs: take the absolute path, replace each separator char
    (\\, /, :) with a single dash, and spaces with dashes.  Consecutive dashes
    are NOT collapsed — e.g. ``G:\\`` becomes ``G---``.
    """
    normalized = os.path.abspath(project_path)
    slug = ""
    for ch in normalized:
        if ch in ("/", "\\", ":", "_", " "):
            slug += "-"
        else:
            slug += ch
    return slug.strip("-")


def discover_memory_dir(project_path: str | None = None) -> Path:
    """Find the Claude Code memory directory for a project.

    Raises FileNotFoundError if no suitable directory is found.
    """
    candidates: list[Path] = []

    if project_path:
        slug = _compute_project_slug(project_path)
        candidates.append(Path.home() / ".claude" / "projects" / slug / "memory")

    env_dir = os.environ.get("CLAUDE_MEMORY_DIR")
    if env_dir:
        candidates.append(Path(env_dir))

    for candidate in candidates:
        if candidate.is_dir():
            return candidate

    # If we have a project_path, create the directory
    if project_path:
        slug = _compute_project_slug(project_path)
        mem_dir = Path.home() / ".claude" / "projects" / slug / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        return mem_dir

    if env_dir:
        p = Path(env_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    raise FileNotFoundError(
        "Could not discover Claude Code memory directory. "
        "Provide --project or set CLAUDE_MEMORY_DIR."
    )


# ---------------------------------------------------------------------------
# Claim -> memory file conversion
# ---------------------------------------------------------------------------

def claim_to_memory_file(claim: dict) -> tuple[str, str, str]:
    """Convert a claim dict to (filename, file_content, index_line).

    Returns:
        filename: e.g. mm_42_supabase-rls-policies.md
        content: YAML frontmatter + body
        index_line: markdown link for MEMORY.md index
    """
    claim_id = claim.get("id", 0)
    text = claim.get("text") or ""
    quality = float(claim.get("quality_score") or 0.0)
    tier = claim.get("tier") or "working"
    dream_type = claim_to_dream_type(claim)

    # Build name/description from text
    name = text[:60].strip()
    if len(text) > 60:
        name = name.rsplit(" ", 1)[0] + "..."
    description = text[:120].strip()

    # Filename
    slug = _slugify(text[:50])
    filename = f"mm_{claim_id}_{slug}.md" if slug else f"mm_{claim_id}.md"

    # Frontmatter
    meta = {
        "name": name,
        "description": description,
        "type": dream_type,
    }

    # Body
    body_lines: list[str] = []

    if dream_type == "feedback":
        # Extract why/how from text
        body_lines.append(text)
        body_lines.append("")
        body_lines.append(f"**Why:** Extracted from project memory (quality {quality:.2f})")
        body_lines.append(f"**How to apply:** {_extract_applicability(claim)}")
    elif dream_type == "project":
        body_lines.append(text)
        created = claim.get("created_at") or ""
        if created:
            body_lines.append("")
            body_lines.append(f"*Date: {created}*")
    else:
        body_lines.append(text)

    body_lines.append("")
    body_lines.append(f"*Source: MemoryMaster claim #{claim_id}, quality: {quality:.2f}, tier: {tier}*")

    content = _dump_frontmatter(meta) + "\n\n" + "\n".join(body_lines) + "\n"

    index_line = f"- [{filename}]({filename}) — {description}"
    return filename, content, index_line


def _extract_applicability(claim: dict) -> str:
    """Build a short applicability note from claim metadata."""
    scope = claim.get("scope") or "project"
    subject = claim.get("subject") or ""
    if subject:
        return f"When working with {subject} (scope: {scope})"
    return f"Within {scope} scope"


# ---------------------------------------------------------------------------
# MEMORY.md index management
# ---------------------------------------------------------------------------

def _read_memory_index(memory_dir: Path) -> list[str]:
    """Read MEMORY.md lines, returning empty list if not found."""
    index_path = memory_dir / "MEMORY.md"
    if not index_path.exists():
        return []
    return index_path.read_text(encoding="utf-8").splitlines()


def _write_memory_index(memory_dir: Path, lines: list[str]) -> None:
    """Write MEMORY.md, respecting the 200-line limit."""
    # Truncate to 200 lines max
    capped = lines[:200]
    index_path = memory_dir / "MEMORY.md"
    index_path.write_text("\n".join(capped) + "\n", encoding="utf-8")


def _split_index_entries(lines: list[str]) -> tuple[list[str], list[str]]:
    """Split MEMORY.md lines into non-mm_ entries and mm_ entries."""
    non_mm: list[str] = []
    mm_entries: list[str] = []
    for line in lines:
        if re.match(r"^- \[mm_", line):
            mm_entries.append(line)
        else:
            non_mm.append(line)
    return non_mm, mm_entries


def _existing_mm_files(lines: list[str]) -> set[str]:
    """Extract mm_*.md filenames already present in MEMORY.md."""
    files: set[str] = set()
    for line in lines:
        match = re.match(r"^- \[(mm_\d+_[^\]]*\.md)\]", line)
        if match:
            files.add(match.group(1))
    return files


# ---------------------------------------------------------------------------
# Lock file check
# ---------------------------------------------------------------------------

def _check_dream_lock(project_path: str | None) -> bool:
    """Return True if a .dream.lock file exists (Auto Dream is running)."""
    if not project_path:
        return False
    slug = _compute_project_slug(project_path)
    lock_path = Path.home() / ".claude" / "projects" / slug / ".dream.lock"
    return lock_path.exists()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _open_db(db_path: str) -> sqlite3.Connection:
    """Open SQLite connection with row_factory."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _query_exportable_claims(
    conn: sqlite3.Connection,
    min_tier: int = 2,
    min_quality: float = 0.5,
    max_memories: int = 50,
) -> list[dict]:
    """Query claims suitable for export.

    Filters by tier (1=core, 2=working, 3=peripheral), quality_score, and
    excludes archived/candidate status. Orders by quality then access_count.
    """
    tier_map = {1: "core", 2: "working", 3: "peripheral"}
    allowed_tiers = [tier_map[t] for t in range(1, min_tier + 1) if t in tier_map]
    if not allowed_tiers:
        allowed_tiers = ["core", "working"]

    placeholders = ",".join("?" for _ in allowed_tiers)
    sql = (
        f"SELECT * FROM claims "
        f"WHERE status IN ('confirmed', 'stale', 'conflicted') "
        f"AND tier IN ({placeholders}) "
        f"AND COALESCE(quality_score, 0.0) >= ? "
        f"ORDER BY COALESCE(quality_score, 0.0) DESC, access_count DESC "
        f"LIMIT ?"
    )
    params = [*allowed_tiers, min_quality, max_memories]

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        # quality_score column may not exist in older schemas
        sql_fallback = (
            f"SELECT * FROM claims "
            f"WHERE status IN ('confirmed', 'stale', 'conflicted') "
            f"AND tier IN ({placeholders}) "
            f"ORDER BY confidence DESC, access_count DESC "
            f"LIMIT ?"
        )
        rows = conn.execute(sql_fallback, [*allowed_tiers, max_memories]).fetchall()

    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Main functions
# ---------------------------------------------------------------------------

def dream_seed(
    db_path: str,
    project_path: str | None = None,
    min_tier: int = 2,
    min_quality: float = 0.5,
    max_memories: int = 50,
    dry_run: bool = False,
) -> dict:
    """Export MemoryMaster claims into Claude Code Auto Dream memory files.

    Returns stats dict with seeded/skipped/total counts.
    """
    if _check_dream_lock(project_path):
        return {
            "error": "Auto Dream lock file detected — aborting to avoid conflicts.",
            "seeded": 0,
            "skipped": 0,
            "total_claims": 0,
            "memory_dir": "",
        }

    conn = _open_db(db_path)
    try:
        claims = _query_exportable_claims(conn, min_tier, min_quality, max_memories)
    finally:
        conn.close()

    memory_dir = discover_memory_dir(project_path)
    memory_dir.mkdir(parents=True, exist_ok=True)

    existing_lines = _read_memory_index(memory_dir)
    already_seeded = _existing_mm_files(existing_lines)
    non_mm_lines, _ = _split_index_entries(existing_lines)

    seeded = 0
    skipped = 0
    new_index_lines: list[str] = []
    seen_texts: list[str] = []

    for claim in claims:
        text = claim.get("text") or ""
        if _is_sensitive(text):
            skipped += 1
            continue

        if _is_near_duplicate(text, seen_texts):
            skipped += 1
            continue

        filename, content, index_line = claim_to_memory_file(claim)

        if filename in already_seeded:
            # Still add to index for consistency
            new_index_lines.append(index_line)
            seen_texts.append(text)
            skipped += 1
            continue

        if not dry_run:
            file_path = memory_dir / filename
            file_path.write_text(content, encoding="utf-8")

        new_index_lines.append(index_line)
        seen_texts.append(text)
        seeded += 1

    # Rebuild MEMORY.md: preserve non-mm entries, add mm entries
    if not dry_run:
        combined = non_mm_lines + new_index_lines
        _write_memory_index(memory_dir, combined)

    return {
        "seeded": seeded,
        "skipped": skipped,
        "total_claims": len(claims),
        "memory_dir": str(memory_dir),
        "dry_run": dry_run,
    }


def dream_ingest(
    db_path: str,
    project_path: str | None = None,
) -> dict:
    """Import Auto Dream memories (non-mm_ files) back into MemoryMaster.

    Returns stats dict with ingested/skipped counts.
    """
    memory_dir = discover_memory_dir(project_path)

    md_files = sorted(memory_dir.glob("*.md"))
    ingested = 0
    skipped = 0

    conn = _open_db(db_path)
    try:
        for md_file in md_files:
            # Skip our own exports and the index
            if md_file.name.startswith("mm_") or md_file.name == "MEMORY.md":
                skipped += 1
                continue

            content = md_file.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(content)

            if not body.strip():
                skipped += 1
                continue

            dream_type = meta.get("type", "project")
            name = meta.get("name", md_file.stem)
            description = meta.get("description", "")

            # Check for duplicates by looking for source marker
            source_marker = f"auto-dream:{md_file.name}"
            existing = conn.execute(
                "SELECT id FROM claims WHERE idempotency_key = ? LIMIT 1",
                (source_marker,),
            ).fetchone()
            if existing:
                skipped += 1
                continue

            # Build claim text from body (strip source lines)
            claim_text = body.strip()
            if len(claim_text) > 2000:
                claim_text = claim_text[:2000]

            # Map dream type to claim_type
            claim_type_map = {
                "feedback": "preference",
                "project": "fact",
                "user": "identity",
                "reference": "reference",
            }
            claim_type = claim_type_map.get(dream_type, "fact")

            try:
                conn.execute(
                    "INSERT INTO claims (text, claim_type, subject, status, confidence, "
                    "scope, volatility, idempotency_key, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'candidate', 0.5, 'project', 'medium', ?, "
                    "datetime('now'), datetime('now'))",
                    (claim_text, claim_type, name, source_marker),
                )
                conn.execute(
                    "INSERT INTO events (claim_id, event_type, details, created_at) "
                    "VALUES (last_insert_rowid(), 'ingest', ?, datetime('now'))",
                    (f"auto-dream import from {md_file.name}",),
                )
                ingested += 1
            except sqlite3.IntegrityError:
                skipped += 1

        conn.commit()
    finally:
        conn.close()

    return {
        "ingested": ingested,
        "skipped": skipped,
        "memory_dir": str(memory_dir),
    }


def dream_sync(
    db_path: str,
    project_path: str | None = None,
    **kwargs,
) -> dict:
    """Bidirectional sync: import Auto Dream -> MemoryMaster, then export MemoryMaster -> Auto Dream.

    Returns combined stats from both operations.
    """
    ingest_stats = dream_ingest(db_path, project_path)
    seed_stats = dream_seed(db_path, project_path, **kwargs)

    return {
        "ingest": ingest_stats,
        "seed": seed_stats,
    }


def dream_clean(
    project_path: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Remove all mm_-prefixed files from the Claude Code memory directory.

    Returns stats dict with removed count and file list.
    """
    memory_dir = discover_memory_dir(project_path)

    mm_files = sorted(memory_dir.glob("mm_*.md"))
    removed_names: list[str] = []

    for f in mm_files:
        removed_names.append(f.name)
        if not dry_run:
            f.unlink()

    # Update MEMORY.md: remove mm_ entries
    if not dry_run:
        existing_lines = _read_memory_index(memory_dir)
        non_mm_lines, _ = _split_index_entries(existing_lines)
        _write_memory_index(memory_dir, non_mm_lines)

    return {
        "removed": len(removed_names),
        "files": removed_names,
        "memory_dir": str(memory_dir),
        "dry_run": dry_run,
    }
