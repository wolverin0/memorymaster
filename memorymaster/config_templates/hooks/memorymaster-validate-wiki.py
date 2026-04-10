#!/usr/bin/env python3
"""Validate wiki article hygiene after Write/Edit.

Fires on Edit|Write of .md files inside obsidian-vault/wiki/. Checks:
  1. YAML frontmatter present
  2. Required fields: title, description (~150 chars), type, scope, date
  3. At least one [[wikilink]] if content > 300 chars (orphan rule)
  4. description field is 50-300 chars (progressive disclosure)

Mirrors obsidian-mind validate-write.py pattern but scoped to MemoryMaster
wiki articles only. Returns warnings via hookSpecificOutput.additionalContext
so Claude fixes in-the-moment.
"""
import json
import sys
import os
from pathlib import Path


REQUIRED_FIELDS = ["title", "type", "scope"]
RECOMMENDED_FIELDS = ["description", "date", "tags"]

# Files that are index/nav pages — exempt from the wikilink rule
EXEMPT_BASENAMES = {"_index.md", "MEMORY.md", "README.md", "log.md"}


def _get_file_path(input_data: dict) -> str | None:
    tool_input = input_data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    fp = tool_input.get("file_path") or ""
    return fp if isinstance(fp, str) else None


def _is_wiki_article(file_path: str) -> bool:
    if not file_path.endswith(".md"):
        return False
    normalized = file_path.replace("\\", "/").lower()
    if "obsidian-vault/wiki/" not in normalized:
        return False
    basename = os.path.basename(normalized)
    if basename in {b.lower() for b in EXEMPT_BASENAMES}:
        return False
    return True


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Return (fields_dict, body). Empty dict if no frontmatter."""
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    fm_text = parts[1]
    body = parts[2]
    fields = {}
    for line in fm_text.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fields[key.strip()] = val.strip()
    return fields, body


def _validate(file_path: str) -> list[str]:
    warnings: list[str] = []
    try:
        content = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    if not content.strip():
        return []

    fields, body = _parse_frontmatter(content)

    if not fields:
        warnings.append("Missing YAML frontmatter (must start with ---)")
        return warnings  # No point checking individual fields

    for req in REQUIRED_FIELDS:
        if req not in fields:
            warnings.append(f"Missing required frontmatter field: `{req}`")

    for rec in RECOMMENDED_FIELDS:
        if rec not in fields:
            warnings.append(
                f"Missing recommended field: `{rec}` — "
                "needed for Bases views and progressive disclosure"
            )

    # description length check
    desc = fields.get("description", "")
    if desc:
        # Strip surrounding quotes if present
        desc_clean = desc.strip('"\'')
        dlen = len(desc_clean)
        if dlen < 30:
            warnings.append(
                f"`description` too short ({dlen} chars) — "
                "should be 50-200 chars for progressive disclosure"
            )
        elif dlen > 300:
            warnings.append(
                f"`description` too long ({dlen} chars) — "
                "keep under 200 chars; move detail to body"
            )

    # Orphan check — content > 300 chars must link to something
    body_text = body.strip()
    if len(body_text) > 300 and "[[" not in body_text:
        warnings.append(
            "Orphan article — no [[wikilinks]] found. "
            "Every wiki article must link to at least one other article."
        )

    return warnings


def main():
    try:
        input_data = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        sys.exit(0)

    file_path = _get_file_path(input_data)
    if not file_path or not _is_wiki_article(file_path):
        sys.exit(0)

    try:
        warnings = _validate(file_path)
    except Exception:
        sys.exit(0)

    if not warnings:
        sys.exit(0)

    basename = os.path.basename(file_path)
    hint_list = "\n".join(f"  - {w}" for w in warnings)
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                f"[MemoryMaster wiki hygiene] `{basename}`:\n{hint_list}\n"
                "Fix these before moving on — orphan articles and missing "
                "metadata break wiki navigation and Bases views."
            ),
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
