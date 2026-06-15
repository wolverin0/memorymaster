"""Wiki article validator + auto-fixer (v3.9.0 F4, ported from gbrain v0.22.4).

Deprecated: P2 census found zero package importers — the installed
memorymaster-validate-wiki hook reimplements validation inline and does NOT
import this module (test-only surface plus own ``__main__``). Kept per operator
verdict (KEEP-DEPRECATED, 2026-06-10); wire-or-remove decision deferred to P5 review.

Inspects an Obsidian-vault wiki article (.md with YAML frontmatter) and
reports schema violations. With ``--fix``, auto-corrects the four fixable
codes; for the rest, exits non-zero so a CI/git-hook gate can refuse the
write.

Validation codes
----------------
* ``MISSING_OPEN`` (fixable) — file does not start with ``---``.
* ``MISSING_CLOSE`` (fixable) — opening ``---`` not closed by another ``---``.
* ``EMPTY_FRONTMATTER`` (fixable) — opens + closes with no content between.
* ``MISSING_REQUIRED:<field>`` — required field absent (``title``/``type``/``scope``).
  ``title`` is fixable from the filename. The other two REQUIRE human input
  and are reported only.
* ``MISSING_RECOMMENDED:<field>`` (fixable) — ``description`` / ``date`` /
  ``tags`` absent. Auto-fix derives a default (current date for ``date``,
  empty list for ``tags``, derived from body for ``description``).
* ``DESCRIPTION_TOO_SHORT`` / ``DESCRIPTION_TOO_LONG`` — outside 50-200 chars.
  Reported only; truncation/expansion is judgement work the human owns.
* ``ORPHAN`` — body > 300 chars and contains no ``[[wikilinks]]``. Reported only.
* ``YAML_PARSE`` — frontmatter present but not parseable. Reported only —
  auto-fix would risk mangling the article.

Exit codes
----------
* 0 — file is valid (or all reported issues were auto-fixed when ``--fix``).
* 1 — file has unfixable errors.
* 2 — bad CLI usage.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

REQUIRED_FIELDS = ("title", "type", "scope")
RECOMMENDED_FIELDS = ("description", "date", "tags")

# Codes that --fix can repair without losing information
FIXABLE_CODES = frozenset(
    {
        "MISSING_OPEN",
        "MISSING_CLOSE",
        "EMPTY_FRONTMATTER",
        "MISSING_REQUIRED:title",  # derivable from filename
        "MISSING_RECOMMENDED:description",
        "MISSING_RECOMMENDED:date",
        "MISSING_RECOMMENDED:tags",
    }
)


@dataclass
class ValidationResult:
    path: str
    codes: list[str] = field(default_factory=list)
    fixed_codes: list[str] = field(default_factory=list)
    fields_after: dict | None = None

    @property
    def ok(self) -> bool:
        # ok if every reported code was auto-fixed and nothing remains
        unfixed = set(self.codes) - set(self.fixed_codes)
        return not unfixed

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "codes": self.codes,
            "fixed": self.fixed_codes,
            "ok": self.ok,
        }


def _parse_frontmatter(content: str) -> tuple[dict | None, str | None, list[str]]:
    """Return (fields, body, codes).

    ``fields`` is a dict when frontmatter parsed cleanly, ``None`` when the
    structural codes (MISSING_OPEN/MISSING_CLOSE/EMPTY/YAML_PARSE) prevent
    parsing. ``body`` is the post-frontmatter content (or the whole file
    when MISSING_OPEN). ``codes`` lists the structural problems.
    """
    codes: list[str] = []
    if not content.startswith("---"):
        return None, content, ["MISSING_OPEN"]

    rest = content[len("---"):].lstrip("\n")
    closing = rest.find("\n---")
    if closing == -1:
        # Try same-line close: e.g. "---\n---" with no body.
        closing = rest.find("---")
        if closing == -1:
            return None, content, ["MISSING_CLOSE"]

    fm_text = rest[:closing].strip()
    body = rest[closing:].lstrip("-").lstrip("\n")

    if not fm_text:
        return {}, body, ["EMPTY_FRONTMATTER"]

    fields: dict = {}
    for line in fm_text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            fields[key] = value
    if not fields:
        return None, body, ["YAML_PARSE"]
    return fields, body, codes


def _validate_fields(fields: dict | None, body: str) -> list[str]:
    codes: list[str] = []
    if fields is None:
        return codes
    for required in REQUIRED_FIELDS:
        if not fields.get(required):
            codes.append(f"MISSING_REQUIRED:{required}")
    for recommended in RECOMMENDED_FIELDS:
        if not fields.get(recommended):
            codes.append(f"MISSING_RECOMMENDED:{recommended}")
    desc = fields.get("description") or ""
    if desc:
        if len(desc) < 50:
            codes.append("DESCRIPTION_TOO_SHORT")
        elif len(desc) > 200:
            codes.append("DESCRIPTION_TOO_LONG")
    if len(body) > 300 and "[[" not in body:
        codes.append("ORPHAN")
    return codes


def validate_file(path: str | os.PathLike[str]) -> ValidationResult:
    p = Path(path)
    result = ValidationResult(path=str(p))
    if not p.is_file():
        result.codes.append("FILE_NOT_FOUND")
        return result
    content = p.read_text(encoding="utf-8", errors="replace")
    fields, body, structural = _parse_frontmatter(content)
    result.codes.extend(structural)
    result.codes.extend(_validate_fields(fields, body or ""))
    result.fields_after = fields
    return result


def _derive_title(path: Path) -> str:
    return path.stem.replace("-", " ").replace("_", " ").title()


def _derive_description(body: str) -> str:
    """Take the first sentence/paragraph of body, trimmed to 50-180 chars."""
    text = body.strip()
    if not text:
        return "Auto-generated wiki article. Description pending human review."
    # First non-empty line
    for line in text.splitlines():
        line = line.strip().lstrip("# ").strip()
        if len(line) >= 30:
            return (line[:200] + "...") if len(line) > 200 else line
    return text[:180].strip()


def auto_fix(path: str | os.PathLike[str]) -> ValidationResult:
    """Apply the fixable codes in-place (with .bak backup)."""
    p = Path(path)
    result = ValidationResult(path=str(p))
    if not p.is_file():
        result.codes.append("FILE_NOT_FOUND")
        return result

    original = p.read_text(encoding="utf-8", errors="replace")
    fields, body, structural = _parse_frontmatter(original)

    work_fields: dict = dict(fields or {})
    work_body = body if body is not None else original
    fixed: list[str] = []

    if "MISSING_OPEN" in structural or "MISSING_CLOSE" in structural or "EMPTY_FRONTMATTER" in structural:
        fixed.extend([c for c in structural if c in FIXABLE_CODES])

    # Derive defaults for missing fields
    if not work_fields.get("title"):
        work_fields["title"] = _derive_title(p)
        fixed.append("MISSING_REQUIRED:title")
    if not work_fields.get("description"):
        work_fields["description"] = _derive_description(work_body or "")
        fixed.append("MISSING_RECOMMENDED:description")
    if not work_fields.get("date"):
        work_fields["date"] = datetime.date.today().isoformat()
        fixed.append("MISSING_RECOMMENDED:date")
    if not work_fields.get("tags"):
        work_fields["tags"] = "[]"
        fixed.append("MISSING_RECOMMENDED:tags")

    # Re-emit frontmatter
    fm_lines = ["---"]
    for k in [
        "title",
        "description",
        "type",
        "scope",
        "tags",
        "date",
        "claims",
        "created",
        "last_updated",
        "related",
    ]:
        v = work_fields.get(k)
        if v in (None, ""):
            continue
        if k in ("tags", "claims", "related") and not str(v).startswith("["):
            v = f"[{v}]"
        fm_lines.append(f"{k}: {v}" if not isinstance(v, str) or '"' in v else f'{k}: "{v}"' if k == "description" else f"{k}: {v}")
    # de-dup any keys not in canonical list — preserve them at the end
    canonical = {"title", "description", "type", "scope", "tags", "date", "claims", "created", "last_updated", "related"}
    for k, v in work_fields.items():
        if k in canonical or v in (None, ""):
            continue
        fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")

    new_content = "\n".join(fm_lines) + "\n\n" + (work_body or "").lstrip("\n")

    # Backup + write
    backup = p.with_suffix(p.suffix + ".bak")
    shutil.copy2(p, backup)
    p.write_text(new_content, encoding="utf-8")

    result.codes = structural + _validate_fields(fields, work_body or "")
    result.fixed_codes = fixed
    result.fields_after = work_fields
    return result


def audit(root: str | os.PathLike[str]) -> list[ValidationResult]:
    """Walk a vault directory, validate every .md (excluding _index/MEMORY/README/log)."""
    rootp = Path(root)
    out: list[ValidationResult] = []
    if not rootp.is_dir():
        return out
    exempt = {"_index.md", "MEMORY.md", "README.md", "log.md"}
    for md in rootp.rglob("*.md"):
        if md.name in exempt:
            continue
        out.append(validate_file(md))
    return out


# --- CLI -------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memorymaster wiki-validate",
        description="Validate (and optionally auto-fix) wiki article frontmatter.",
    )
    p.add_argument("path", help="Path to a wiki .md file or vault root (with --audit)")
    p.add_argument("--fix", action="store_true", help="Auto-fix the 4 fixable codes (creates .bak backup)")
    p.add_argument("--audit", action="store_true", help="Walk the vault directory and validate every article")
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.audit:
        results = audit(args.path)
    elif args.fix:
        results = [auto_fix(args.path)]
    else:
        results = [validate_file(args.path)]

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        for r in results:
            status = "OK" if r.ok else "FAIL"
            print(f"[{status}] {r.path}")
            for c in r.codes:
                marker = "  fixed" if c in r.fixed_codes else "  ✗"
                print(f"{marker} {c}")

    any_fail = any(not r.ok for r in results)
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
