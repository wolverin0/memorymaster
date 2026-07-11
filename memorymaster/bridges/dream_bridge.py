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
from pathlib import Path

from memorymaster.core import observability, spool
from memorymaster.stores._storage_shared import open_conn
from memorymaster.core.security import redact_text as _redact_text
from memorymaster.core.security import sanitize_claim_input

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sensitivity filter — credential detection is delegated to
# `memorymaster.core.security.redact_text` (single source of truth). Below we keep
# only the dream-bridge-specific extras: personal paths, SSH command shapes,
# and vendor-specific keys that security.py doesn't need for general ingest
# filtering but we want to block from being seeded into dream memory.
# ---------------------------------------------------------------------------

_DREAM_EXTRA_PATTERNS = re.compile(
    r"(?i)"
    r"\[REDACTED"
    # Personal/user-specific paths
    r"|[A-Z]:\\.*\\(?:OneDrive|Users\\[a-z])"
    r"|/home/\w+"
    # SSH/SCP command shapes — not credentials but memory-leaking host refs
    r"|ssh\s+.*\w+@"
    r"|sshpass\b"
    r"|scp\s+.*@"
    # Public IPs in connection strings (non-localhost, non-example)
    r"|\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+"
    r"|ubuntu@\d{1,3}\.\d{1,3}"
    # Private IPs should not cross the dream-memory boundary.
    r"|\b(?:10\.(?:\d{1,3}\.){2}\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3})\b"
    # Deployment artefacts
    r"|cat\s*>\s*/tmp/.*(?:pw|pass|key|cred)"
    r"|ENDPW"
    r"|esxi_pass"
    # Webhook URLs with tokens embedded
    r"|webhook.*(?:token|key|secret).*[A-Za-z0-9]{20,}"
    # ENV var assignments for known secret variables
    r"|(?:export\s+)?(?:DB_PASS|MYSQL_PASSWORD|POSTGRES_PASSWORD|SSH_PASS)\s*="
    # Vendor-specific API keys not in the canonical filter
    r"|sbp_[A-Za-z0-9]{20,}"
    r"|sk_(?:live|test)_[A-Za-z0-9]{20,}"
    r"|SG\.[A-Za-z0-9\-_]{20,}"
    r"|AC[a-f0-9]{32}"
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
    # Generic install/run instructions (not useful as memory)
    r"|^(?:pip install|npm install|yarn add|uvicorn |python -m )"
    # Environment variable templates (placeholder values)
    r"|(?:VITE_|NEXT_PUBLIC_)\w+=(?:https?://xxx|xxx)"
    # Test health endpoint snippets
    r"|^##\s*\d+\.\s*(?:Test|Build|Run|Install|Deploy)\s"
    # "Or run locally" / generic setup instructions
    r"|^##\s*(?:Or run|Getting [Ss]tarted|Prerequisites|Environment [Vv]ariables)"
    # Support/docs links only (no actual knowledge)
    r"|^##\s*(?:Support|Sources|Documentation|References)\b"
    # Duplicate doc/readme boilerplate (links-only claims)
    r"|^Documentation for .* is available at https?://"
    # API endpoint snippets with no context
    r"|requests\.get\s*\(\s*['\"]http://localhost"
    # Monitoring CLI commands without context
    r"|^##\s*Monitoring\s+.*(?:board|live|terminal)"
)


def _is_sensitive(text: str) -> bool:
    """Return True if text contains credentials, private IPs, or is just noise."""
    if not text:
        return False
    # Canonical credential/secret filter (shared with ingest + MCP + storage)
    _, findings = _redact_text(text)
    if findings:
        return True
    # Dream-bridge-specific extras (personal paths, SSH shapes, vendor keys)
    if _DREAM_EXTRA_PATTERNS.search(text):
        return True
    if _NOISE_PATTERNS.search(text.strip()):
        return True
    # Reject texts that are mostly code (>60% lines start with common code chars)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 3:
        code_lines = sum(1 for line in lines if line.startswith(("$", "#!", "cd ", "pip ", "npm ",
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
    """Open SQLite connection with the uniform writer envelope."""
    return open_conn(db_path)


def _query_exportable_claims(
    conn: sqlite3.Connection,
    min_tier: int = 2,
    min_quality: float = 0.5,
    max_memories: int = 50,
    scope_filter: str | None = None,
) -> list[dict]:
    """Query claims suitable for export.

    Filters by tier (1=core, 2=working, 3=peripheral), quality_score, scope,
    and excludes archived/candidate status. Orders by quality then access_count.
    """
    tier_map = {1: "core", 2: "working", 3: "peripheral"}
    allowed_tiers = [tier_map[t] for t in range(1, min_tier + 1) if t in tier_map]
    if not allowed_tiers:
        allowed_tiers = ["core", "working"]

    # Fetch a larger pool (5x) so sensitivity filtering still yields enough results
    fetch_limit = max_memories * 5
    placeholders = ",".join("?" for _ in allowed_tiers)

    # Scope filter: only export claims from the current project
    scope_clause = ""
    scope_params: list = []
    if scope_filter:
        scope_clause = " AND scope LIKE ? "
        scope_params = [f"{scope_filter}%"]

    sql = (
        f"SELECT * FROM claims "
        f"WHERE status IN ('confirmed', 'stale', 'conflicted') "
        f"AND tier IN ({placeholders}) "
        f"AND COALESCE(quality_score, 0.0) >= ? "
        f"{scope_clause}"
        f"ORDER BY COALESCE(quality_score, 0.0) DESC, access_count DESC "
        f"LIMIT ?"
    )
    params = [*allowed_tiers, min_quality, *scope_params, fetch_limit]

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        # quality_score column may not exist in older schemas
        sql_fallback = (
            f"SELECT * FROM claims "
            f"WHERE status IN ('confirmed', 'stale', 'conflicted') "
            f"AND tier IN ({placeholders}) "
            f"{scope_clause}"
            f"ORDER BY confidence DESC, access_count DESC "
            f"LIMIT ?"
        )
        rows = conn.execute(sql_fallback, [*allowed_tiers, *scope_params, fetch_limit]).fetchall()

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

    # Derive scope from project path to avoid cross-project pollution
    scope_filter = None
    if project_path:
        project_name = os.path.basename(project_path).lower().replace(" ", "-")
        scope_filter = f"project:{project_name}"

    conn = _open_db(db_path)
    try:
        claims = _query_exportable_claims(conn, min_tier, min_quality, max_memories, scope_filter=scope_filter)
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
        if seeded >= max_memories:
            break

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


# Map Auto Dream memory types to MemoryMaster claim types (shared by the
# direct-INSERT path and the spool path — see _parse_dream_file).
_DREAM_CLAIM_TYPE_MAP = {
    "feedback": "preference",
    "project": "fact",
    "user": "identity",
    "reference": "reference",
}


def _parse_dream_file(md_file: Path) -> dict | None:
    """Parse one Auto Dream memory file into claim fields, or None to skip.

    Shared by the direct-INSERT path and the spool path (spec §2.3) so the
    sensitivity filter, subject redaction, truncation, and type mapping can
    never drift between the two regimes — a filter applied on one path but
    not the other would turn the flag flip into a silent secret-leak channel.
    """
    # Skip our own exports and the index
    if md_file.name.startswith("mm_") or md_file.name == "MEMORY.md":
        return None

    content = md_file.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(content)

    if not body.strip():
        return None

    # Build claim text from body (strip source lines)
    claim_text = body.strip()
    if _is_sensitive(claim_text):
        return None

    # The subject comes from the frontmatter ``name:`` field, which is
    # NOT covered by the claim_text filter above. A token or personal
    # path smuggled in via ``name:`` would otherwise be persisted
    # verbatim on every dream_sync (sensitivity-filter invariant 1).
    # Reject the whole file if the subject is sensitive, and redact any
    # secret substrings before storing it as the claim subject.
    name = meta.get("name", md_file.stem)
    if _is_sensitive(name):
        return None
    name, _name_findings = _redact_text(name)

    if len(claim_text) > 2000:
        claim_text = claim_text[:2000]

    return {
        "text": claim_text,
        "claim_type": _DREAM_CLAIM_TYPE_MAP.get(meta.get("type", "project"), "fact"),
        "subject": name,
        "source_marker": f"auto-dream:{md_file.name}",
    }


def _dream_ingest_spool(db_path: str, md_files: list[Path], memory_dir: Path) -> dict:
    """Append Auto Dream items as ``op:"dream"`` spool envelopes (spec §2.3).

    No DB is opened — that is the point: under the flag the dream bridge
    leaves the writer set and the session-end hook drops from a multi-GB DB
    open + INSERT to a file append. Dedup moves to drain time: the
    envelope's idempotency_key (``auto-dream:<filename>``) hits svc.ingest's
    dedup, so re-spooling the same file on every session end stays a no-op.
    """
    spooled = 0
    skipped = 0
    for md_file in md_files:
        claim = _parse_dream_file(md_file)
        if claim is None:
            skipped += 1
            continue
        spool.append(
            db_path,
            "dream",
            {
                "text": claim["text"],
                "claim_type": claim["claim_type"],
                "subject": claim["subject"],
                "scope": "project",
                "volatility": "medium",
                "confidence": 0.5,
                "source_agent": "dream-bridge",
                "citations": [{"source": "auto-dream", "locator": md_file.name}],
            },
            idempotency_key=claim["source_marker"],
        )
        spooled += 1

    return {
        "ingested": 0,
        "spooled": spooled,
        "skipped": skipped,
        "memory_dir": str(memory_dir),
    }


def dream_ingest(
    db_path: str,
    project_path: str | None = None,
    *,
    use_spool: bool | None = None,
) -> dict:
    """Import Auto Dream memories (non-mm_ files) back into MemoryMaster.

    Under MEMORYMASTER_WAL_DISCIPLINE=1 (or ``use_spool=True``) items are
    appended to the write spool as ``op:"dream"`` envelopes instead of
    opening the DB (spec §2.3); the steward drain replays them through
    ``svc.ingest``, where the canonical sensitivity filter and the
    idempotency_key dedup apply. Flag off = the untouched direct-INSERT path.

    Returns stats dict with ingested/skipped (direct) or spooled/skipped
    (spool) counts.
    """
    if use_spool is None:
        use_spool = spool.wal_discipline_enabled()

    memory_dir = discover_memory_dir(project_path)
    md_files = sorted(memory_dir.glob("*.md"))

    if use_spool:
        return _dream_ingest_spool(db_path, md_files, memory_dir)

    ingested = 0
    skipped = 0

    conn = _open_db(db_path)
    try:
        for md_file in md_files:
            claim = _parse_dream_file(md_file)
            if claim is None:
                skipped += 1
                continue

            # Check for duplicates by looking for source marker
            existing = conn.execute(
                """
                SELECT id FROM claims
                WHERE idempotency_key = ?
                  AND visibility = 'public'
                  AND tenant_id IS NULL
                  AND scope = 'project'
                LIMIT 1
                """,
                (claim["source_marker"],),
            ).fetchone()
            if existing:
                skipped += 1
                continue

            # Sensitivity firewall — the direct-INSERT path bypasses svc.ingest,
            # so run the SAME canonical filter here (default-deny). A parsed dream
            # note carrying a credential must never reach the claims table.
            sanitized = sanitize_claim_input(
                text=claim["text"],
                object_value=None,
                citations=[],
                subject=claim["subject"],
            )
            if sanitized.is_sensitive:
                observability.bump_claim_filtered("dream_ingest_sensitive")
                log.warning(
                    "dream_ingest: skipped sensitive note %s [REDACTED findings=%s]",
                    md_file.name,
                    ",".join(sanitized.findings),
                )
                skipped += 1
                continue

            try:
                conn.execute(
                    "INSERT INTO claims (text, claim_type, subject, status, confidence, "
                    "scope, volatility, idempotency_key, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'candidate', 0.5, 'project', 'medium', ?, "
                    "datetime('now'), datetime('now'))",
                    (claim["text"], claim["claim_type"], claim["subject"], claim["source_marker"]),
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
