"""Ingest .planning/*.md and CLAUDE.md files from all projects into memorymaster."""

import os
import re
import sys
from pathlib import Path

# Ensure memorymaster is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorymaster.service import MemoryService
from memorymaster.models import CitationInput

PROJECTS_ROOT = Path(os.environ.get("PROJECTS_ROOT", "."))
DB_PATH = Path(__file__).resolve().parent.parent / "memorymaster.db"

# Dirs/files to skip
SKIP_DIRS = {"node_modules", "_archive", "codebase", ".git", "__pycache__"}


def derive_project_name(file_path: Path) -> str:
    """Extract project name from file path relative to PROJECTS_ROOT."""
    rel = file_path.relative_to(PROJECTS_ROOT)
    return rel.parts[0]


def split_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown by ## headers. Returns [(header, body), ...]."""
    # Match ## headers (level 2+)
    pattern = re.compile(r'^(#{1,4})\s+(.+)$', re.MULTILINE)
    matches = list(pattern.finditer(text))

    if not matches:
        # No headers - treat entire file as one section
        return [("Full Document", text.strip())]

    sections = []
    # Content before first header
    preamble = text[:matches[0].start()].strip()
    if preamble and len(preamble) >= 20:
        sections.append(("Preamble", preamble))

    for i, match in enumerate(matches):
        header = match.group(2).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        # Include header in body for context
        full_text = f"## {header}\n\n{body}"
        sections.append((header, full_text))

    return sections


def collect_files() -> list[Path]:
    """Find all CLAUDE.md and .planning/*.md files."""
    files = []

    for entry in PROJECTS_ROOT.iterdir():
        if not entry.is_dir() or entry.name.startswith('.'):
            continue

        # CLAUDE.md at project root
        claude_md = entry / "CLAUDE.md"
        if claude_md.exists():
            files.append(claude_md)

        # CLAUDE.md in .claude/ subdir
        claude_md2 = entry / ".claude" / "CLAUDE.md"
        if claude_md2.exists():
            files.append(claude_md2)

        # .planning/*.md (recursive, but skip codebase/)
        planning_dir = entry / ".planning"
        if planning_dir.exists():
            for md_file in planning_dir.rglob("*.md"):
                # Skip if any parent dir is in SKIP_DIRS
                skip = False
                for part in md_file.relative_to(planning_dir).parts:
                    if part in SKIP_DIRS:
                        skip = True
                        break
                if not skip:
                    files.append(md_file)

        # Also check one level deeper for subproject CLAUDE.md
        for sub in entry.iterdir():
            if not sub.is_dir() or sub.name.startswith('.') or sub.name in SKIP_DIRS:
                continue
            sub_claude = sub / "CLAUDE.md"
            if sub_claude.exists():
                files.append(sub_claude)
            sub_planning = sub / ".planning"
            if sub_planning.exists():
                for md_file in sub_planning.rglob("*.md"):
                    skip = False
                    for part in md_file.relative_to(sub_planning).parts:
                        if part in SKIP_DIRS:
                            skip = True
                            break
                    if not skip:
                        files.append(md_file)

    return sorted(set(files))


def main():
    svc = MemoryService(db_target=str(DB_PATH), workspace_root=Path('.'))
    files = collect_files()
    print(f"Found {len(files)} files to process")

    total_claims = 0
    errors = 0

    for file_path in files:
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"  ERROR reading {file_path}: {e}")
            errors += 1
            continue

        if len(text.strip()) < 20:
            continue

        project_name = derive_project_name(file_path)
        file_name = file_path.name
        sections = split_sections(text)
        file_claims = 0

        for idx, (header, body) in enumerate(sections):
            if len(body.strip()) < 20:
                continue

            truncated = body[:2000]
            idem_key = f"planning-{project_name}-{file_name}-{idx}"
            scope = f"project:{project_name}"

            try:
                svc.ingest(
                    text=truncated,
                    citations=[CitationInput(source=str(file_path), locator=header)],
                    claim_type="fact",
                    scope=scope,
                    confidence=0.7,
                    idempotency_key=idem_key,
                )
                file_claims += 1
            except Exception as e:
                # Likely duplicate idempotency key - that's fine
                err_str = str(e)
                if "idempotency" in err_str.lower() or "unique" in err_str.lower():
                    file_claims += 1  # Already exists, count it
                else:
                    print(f"  ERROR ingesting {idem_key}: {e}")
                    errors += 1

        if file_claims > 0:
            rel_path = file_path.relative_to(PROJECTS_ROOT)
            print(f"Ingested {file_claims} claims from {rel_path}")
            total_claims += file_claims

    print(f"\n{'='*50}")
    print(f"Total claims ingested: {total_claims}")
    print(f"Errors: {errors}")
    print(f"Files processed: {len(files)}")


if __name__ == "__main__":
    main()
