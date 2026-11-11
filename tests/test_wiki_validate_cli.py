"""Tests for v3.9.0 F4 — wiki-validate auto-fix CLI."""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.knowledge.wiki_validate import (
    FIXABLE_CODES,
    audit,
    auto_fix,
    main,
    validate_file,
)


VALID_FRONTMATTER = """---
title: My Article
description: This is a substantial description that meets the 50 to 300 character window required by the schema enforcement layer.
type: decision
scope: project:memorymaster
tags: [decision, fact]
date: 2026-04-27
---

# My Article

Body text here. Links to [[other-article]] satisfy the orphan rule.
"""


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_valid_article_passes(tmp_path):
    p = _write(tmp_path, "ok.md", VALID_FRONTMATTER)
    r = validate_file(p)
    assert r.codes == []
    assert r.ok is True


def test_missing_open_detected(tmp_path):
    p = _write(tmp_path, "no-fm.md", "# Just a title\n\nNo frontmatter here.\n")
    r = validate_file(p)
    assert "MISSING_OPEN" in r.codes


def test_missing_close_detected(tmp_path):
    p = _write(tmp_path, "no-close.md", "---\ntitle: x\n\nbody never closes\n")
    r = validate_file(p)
    assert "MISSING_CLOSE" in r.codes


def test_empty_frontmatter_detected(tmp_path):
    p = _write(tmp_path, "empty.md", "---\n---\n\nbody\n")
    r = validate_file(p)
    assert "EMPTY_FRONTMATTER" in r.codes


def test_missing_required_fields_detected(tmp_path):
    content = """---
description: A description that is long enough to satisfy the 50-300 character requirement window.
date: 2026-04-27
tags: [x]
---

body with [[link]] to satisfy orphan rule.
"""
    p = _write(tmp_path, "missing-req.md", content)
    r = validate_file(p)
    assert "MISSING_REQUIRED:title" in r.codes
    assert "MISSING_REQUIRED:type" in r.codes
    assert "MISSING_REQUIRED:scope" in r.codes


def test_orphan_detected(tmp_path):
    long_body = "x " * 200  # 400 chars, no [[link]]
    content = f"""---
title: x
description: This is a substantial description that meets the 50 to 300 character window required by the schema layer.
type: fact
scope: project:m
tags: [x]
date: 2026-04-27
---

{long_body}
"""
    p = _write(tmp_path, "orphan.md", content)
    r = validate_file(p)
    assert "ORPHAN" in r.codes


def test_description_too_short_detected(tmp_path):
    content = """---
title: x
description: too short
type: fact
scope: project:m
tags: [x]
date: 2026-04-27
---

[[link]] body.
"""
    p = _write(tmp_path, "short-desc.md", content)
    r = validate_file(p)
    assert "DESCRIPTION_TOO_SHORT" in r.codes


def test_auto_fix_creates_backup_and_fills_recommended(tmp_path):
    """Auto-fix on an article missing description/date/tags should fill them
    and write a .bak backup."""
    content = """---
title: x
type: fact
scope: project:m
---

[[link]] body.
"""
    p = _write(tmp_path, "needs-fix.md", content)
    r = auto_fix(p)
    assert (tmp_path / "needs-fix.md.bak").exists()
    assert "MISSING_RECOMMENDED:description" in r.fixed_codes
    assert "MISSING_RECOMMENDED:date" in r.fixed_codes
    assert "MISSING_RECOMMENDED:tags" in r.fixed_codes
    # Re-validate the file to confirm
    r2 = validate_file(p)
    assert "MISSING_RECOMMENDED:description" not in r2.codes
    assert "MISSING_RECOMMENDED:date" not in r2.codes
    assert "MISSING_RECOMMENDED:tags" not in r2.codes


def test_auto_fix_derives_title_from_filename(tmp_path):
    content = """---
description: A description that is long enough to satisfy the 50-300 character requirement window.
type: fact
scope: project:m
date: 2026-04-27
tags: [x]
---

[[link]] body.
"""
    p = _write(tmp_path, "my-cool-article.md", content)
    r = auto_fix(p)
    assert "MISSING_REQUIRED:title" in r.fixed_codes
    after = p.read_text(encoding="utf-8")
    assert 'title: "My Cool Article"' in after or "title: My Cool Article" in after


def test_audit_walks_directory(tmp_path):
    _write(tmp_path, "good.md", VALID_FRONTMATTER)
    _write(tmp_path, "bad.md", "no frontmatter at all\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    _write(sub, "nested.md", VALID_FRONTMATTER)
    # exempt files should NOT be checked
    _write(tmp_path, "_index.md", "no frontmatter")
    _write(tmp_path, "README.md", "no frontmatter")

    results = audit(tmp_path)
    paths = {Path(r.path).name for r in results}
    assert paths == {"good.md", "bad.md", "nested.md"}


def test_fixable_codes_contains_expected_set():
    expected = {
        "MISSING_OPEN",
        "MISSING_CLOSE",
        "EMPTY_FRONTMATTER",
        "MISSING_REQUIRED:title",
        "MISSING_RECOMMENDED:description",
        "MISSING_RECOMMENDED:date",
        "MISSING_RECOMMENDED:tags",
    }
    assert expected == FIXABLE_CODES


def test_cli_returns_zero_for_valid(tmp_path):
    p = _write(tmp_path, "ok.md", VALID_FRONTMATTER)
    rc = main([str(p)])
    assert rc == 0


def test_cli_returns_one_for_invalid(tmp_path):
    p = _write(tmp_path, "bad.md", "no frontmatter\n")
    rc = main([str(p)])
    assert rc == 1
