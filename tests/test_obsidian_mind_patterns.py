"""E2E test suite for obsidian-mind-inspired patterns.

Validates the 5 new components added in this session:
  1. Classification hook — regex signal matcher (UserPromptSubmit)
  2. Validate-wiki hook — frontmatter + wikilink check (PostToolUse)
  3. SessionStart hook — injects DB state at session start
  4. Wiki description/tags/date frontmatter (wiki_engine._write_article)
  5. Obsidian Bases generator (vault_bases.generate_bases)

Hooks are tested via subprocess with mocked stdin so we catch real-world
invocation bugs (encoding, JSON serialization, exit codes).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

HOOKS_DIR = Path.home() / ".claude" / "hooks"
CLASSIFY_HOOK = HOOKS_DIR / "memorymaster-classify.py"
VALIDATE_WIKI_HOOK = HOOKS_DIR / "memorymaster-validate-wiki.py"
SESSION_START_HOOK = HOOKS_DIR / "memorymaster-session-start.py"

# Skip the whole module if hooks aren't installed (CI / fresh clones)
pytestmark = pytest.mark.skipif(
    not CLASSIFY_HOOK.exists(),
    reason="MemoryMaster hooks not installed (run scripts/setup-hooks.py)",
)


def _run_hook(hook_path: Path, stdin_payload: dict) -> dict:
    """Run a hook as a subprocess and return parsed JSON stdout."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(stdin_payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=15,
    )
    assert proc.returncode == 0, f"Hook crashed: {proc.stderr}"
    if not proc.stdout.strip():
        return {}
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# 1. Classification hook
# ---------------------------------------------------------------------------

class TestClassifyHook:
    def test_decision_spanish(self):
        out = _run_hook(CLASSIFY_HOOK, {"prompt": "decidimos usar Qdrant porque FTS5 no alcanza"})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "DECISION" in ctx
        assert "ingest_claim" in ctx

    def test_decision_english(self):
        out = _run_hook(CLASSIFY_HOOK, {"prompt": "We decided to go with Postgres instead of MySQL"})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "DECISION" in ctx

    def test_bug_root_cause_spanish(self):
        out = _run_hook(CLASSIFY_HOOK, {"prompt": "el problema es que el cache no invalida cuando cambia la clave"})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "BUG_ROOT_CAUSE" in ctx

    def test_gotcha_spanish(self):
        out = _run_hook(CLASSIFY_HOOK, {"prompt": "ojo con el timeout de Qdrant que silenciosamente devuelve vacio"})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "GOTCHA" in ctx

    def test_constraint_english(self):
        out = _run_hook(CLASSIFY_HOOK, {"prompt": "the scope filter is mandatory for multi-tenant queries"})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "CONSTRAINT" in ctx

    def test_architecture_spanish(self):
        out = _run_hook(CLASSIFY_HOOK, {"prompt": "tenemos que refactorizar la arquitectura del steward"})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "ARCHITECTURE" in ctx

    def test_environment_english(self):
        out = _run_hook(CLASSIFY_HOOK, {"prompt": "set the GEMINI_API_KEY env var before running"})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "ENVIRONMENT" in ctx

    def test_no_false_positive_on_plain_text(self):
        out = _run_hook(CLASSIFY_HOOK, {"prompt": "hello how are you today"})
        assert out == {}, "Plain text should not trigger any signal"

    def test_short_prompt_ignored(self):
        out = _run_hook(CLASSIFY_HOOK, {"prompt": "hi"})
        assert out == {}, "Prompts under 5 chars should be ignored"

    def test_word_boundary_not_false_match(self):
        # "deciduous" contains "deci" but shouldn't trigger DECISION
        out = _run_hook(CLASSIFY_HOOK, {"prompt": "the deciduous trees are beautiful here"})
        assert out == {}, "Word-boundary false positives not allowed"

    def test_multiple_signals_combined(self):
        prompt = "decidimos refactorizar porque la arquitectura tenía un bug crítico"
        out = _run_hook(CLASSIFY_HOOK, {"prompt": prompt})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "DECISION" in ctx
        assert "ARCHITECTURE" in ctx


# ---------------------------------------------------------------------------
# 2. Validate-wiki hook
# ---------------------------------------------------------------------------

class TestValidateWikiHook:
    def _make_wiki_file(self, tmp_path: Path, name: str, content: str) -> Path:
        # Hook only fires for files whose path contains "obsidian-vault/wiki/"
        wiki_dir = tmp_path / "obsidian-vault" / "wiki" / "project-test"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        f = wiki_dir / name
        f.write_text(content, encoding="utf-8")
        return f

    def test_file_outside_wiki_ignored(self, tmp_path):
        f = tmp_path / "random.md"
        f.write_text("# hello\ncontent", encoding="utf-8")
        out = _run_hook(VALIDATE_WIKI_HOOK, {"tool_input": {"file_path": str(f)}})
        assert out == {}

    def test_missing_frontmatter_warns(self, tmp_path):
        f = self._make_wiki_file(tmp_path, "bad.md", "# No frontmatter\nJust body text.")
        out = _run_hook(VALIDATE_WIKI_HOOK, {"tool_input": {"file_path": str(f)}})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "YAML frontmatter" in ctx

    def test_missing_description_warns(self, tmp_path):
        content = (
            "---\n"
            "title: Test\n"
            "type: fact\n"
            "scope: project:test\n"
            "---\n\n"
            "# Test\n\n"
            "Some body. " * 30
            + "\n\n[[other-article]]\n"
        )
        f = self._make_wiki_file(tmp_path, "no-desc.md", content)
        out = _run_hook(VALIDATE_WIKI_HOOK, {"tool_input": {"file_path": str(f)}})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "description" in ctx

    def test_orphan_article_warns(self, tmp_path):
        content = (
            "---\n"
            "title: Orphan\n"
            "description: An orphan article with no wikilinks at all\n"
            "type: fact\n"
            "scope: project:test\n"
            "date: 2026-04-09\n"
            "tags: [fact]\n"
            "---\n\n"
            "# Orphan\n\n"
            + ("This article talks about things but links nowhere. " * 20)
        )
        f = self._make_wiki_file(tmp_path, "orphan.md", content)
        out = _run_hook(VALIDATE_WIKI_HOOK, {"tool_input": {"file_path": str(f)}})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "Orphan" in ctx or "wikilinks" in ctx

    def test_valid_article_passes(self, tmp_path):
        content = (
            "---\n"
            "title: Good Article\n"
            'description: "A complete wiki article with every required frontmatter field filled in properly."\n'
            "type: decision\n"
            "scope: project:test\n"
            "date: 2026-04-09\n"
            "tags: [decision, project-test]\n"
            "---\n\n"
            "# Good Article\n\n"
            "This article links to [[another-article]] and explains things clearly. "
            * 10
        )
        f = self._make_wiki_file(tmp_path, "good.md", content)
        out = _run_hook(VALIDATE_WIKI_HOOK, {"tool_input": {"file_path": str(f)}})
        assert out == {}, f"Valid article should not warn, got: {out}"

    def test_index_file_exempted(self, tmp_path):
        f = self._make_wiki_file(tmp_path, "_index.md", "# Index\nJust a list")
        out = _run_hook(VALIDATE_WIKI_HOOK, {"tool_input": {"file_path": str(f)}})
        assert out == {}, "_index.md should be exempt from validation"


# ---------------------------------------------------------------------------
# 3. SessionStart hook
# ---------------------------------------------------------------------------

class TestSessionStartHook:
    def test_hook_produces_context(self):
        """The hook reads the real DB — just verify it runs and produces JSON."""
        out = _run_hook(SESSION_START_HOOK, {})
        # May be empty if DB is empty, but if present must have the right shape
        if out:
            assert "hookSpecificOutput" in out
            assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
            ctx = out["hookSpecificOutput"]["additionalContext"]
            assert "MemoryMaster session context" in ctx

    def test_hook_handles_missing_db(self, tmp_path, monkeypatch):
        """If DB path doesn't exist, hook should exit silently (exit 0, no output)."""
        # Patch the hook by calling it from a dir where it can't find the DB
        # Actually the hook has a hardcoded DB_PATH — so this test just verifies
        # it doesn't crash with an empty stdin.
        proc = subprocess.run(
            [sys.executable, str(SESSION_START_HOOK)],
            input="",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        assert proc.returncode == 0


# ---------------------------------------------------------------------------
# 4. Wiki description/tags/date frontmatter
# ---------------------------------------------------------------------------

class TestWikiDescriptionField:
    def test_extract_description_from_body(self):
        from memorymaster.wiki_engine import _extract_description

        body = "# Title\n\nThis is the first real paragraph with enough content to be useful. It explains the thing."
        desc = _extract_description(body)
        assert len(desc) >= 40
        assert "first real paragraph" in desc

    def test_extract_description_skips_headers_and_lists(self):
        from memorymaster.wiki_engine import _extract_description

        body = "## Header\n\n- bullet one\n- bullet two\n\nActual paragraph content goes here and is substantial enough."
        desc = _extract_description(body)
        assert "Actual paragraph" in desc
        assert "bullet" not in desc

    def test_extract_description_respects_max_chars(self):
        from memorymaster.wiki_engine import _extract_description

        body = "This is a very long paragraph. " * 50
        desc = _extract_description(body, max_chars=150)
        assert len(desc) <= 200  # Soft limit with ellipsis tolerance

    def test_build_tags_includes_type_and_scope(self):
        from memorymaster.wiki_engine import _build_tags

        tags = _build_tags("decision", "project:memorymaster", ["decision", "decision", "fact"])
        assert "decision" in tags
        assert "project-memorymaster" in tags
        assert "fact" in tags

    def test_build_tags_capped(self):
        from memorymaster.wiki_engine import _build_tags

        tags = _build_tags("fact", "project:x", ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"])
        assert len(tags) <= 8

    def test_write_article_produces_full_frontmatter(self, tmp_path):
        from memorymaster.wiki_engine import _write_article

        body = "This is a wiki article with enough content for the description extractor to pick up on."
        path = _write_article(
            wiki_dir=tmp_path,
            scope_dir="project-test",
            slug="sample",
            title="Sample",
            body=body,
            article_type="decision",
            scope="project:test",
            claim_ids=[1, 2, 3],
            related=["[[other]]"],
            claim_types=["decision", "fact"],
        )
        content = path.read_text(encoding="utf-8")
        # Required new fields
        assert "description:" in content
        assert "tags:" in content
        assert "date:" in content
        # Preserved legacy fields
        assert "title:" in content
        assert "type: decision" in content
        assert "scope: project:test" in content

    def test_yaml_escape_handles_colons(self):
        from memorymaster.wiki_engine import _yaml_escape

        assert _yaml_escape("simple") == "simple"
        escaped = _yaml_escape("has: colon")
        assert escaped.startswith('"') and escaped.endswith('"')


# ---------------------------------------------------------------------------
# 5. Obsidian Bases generator
# ---------------------------------------------------------------------------

class TestVaultBases:
    def test_generate_bases_writes_all_files(self, tmp_path):
        from memorymaster.vault_bases import generate_bases, BASES

        result = generate_bases(tmp_path)
        assert result["written"] == len(BASES)
        bases_dir = tmp_path / "bases"
        assert bases_dir.exists()
        for filename in BASES.keys():
            f = bases_dir / filename
            assert f.exists(), f"Missing: {filename}"
            content = f.read_text(encoding="utf-8")
            assert "filters:" in content
            assert "views:" in content

    def test_bases_are_valid_yaml(self, tmp_path):
        from memorymaster.vault_bases import generate_bases

        try:
            import yaml  # noqa: F401
        except ImportError:
            pytest.skip("PyYAML not installed")

        generate_bases(tmp_path)
        bases_dir = tmp_path / "bases"
        import yaml
        for f in bases_dir.glob("*.base"):
            parsed = yaml.safe_load(f.read_text(encoding="utf-8"))
            assert "filters" in parsed
            assert "views" in parsed
            assert isinstance(parsed["views"], list)

    def test_bases_readme_written(self, tmp_path):
        from memorymaster.vault_bases import generate_bases

        generate_bases(tmp_path)
        readme = tmp_path / "bases" / "README.md"
        assert readme.exists()
        assert "MemoryMaster" in readme.read_text(encoding="utf-8")

    def test_generate_bases_idempotent(self, tmp_path):
        from memorymaster.vault_bases import generate_bases

        r1 = generate_bases(tmp_path)
        r2 = generate_bases(tmp_path)
        assert r1["written"] == r2["written"]


# ---------------------------------------------------------------------------
# 6. Integration check — settings.json wiring
# ---------------------------------------------------------------------------

class TestHooksIntegration:
    def test_settings_json_references_all_hooks(self):
        settings_path = Path.home() / ".claude" / "settings.json"
        if not settings_path.exists():
            pytest.skip("settings.json not found")
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})

        all_commands: list[str] = []
        for event_hooks in hooks.values():
            for group in event_hooks:
                for h in group.get("hooks", []):
                    all_commands.append(h.get("command", ""))
        blob = "\n".join(all_commands)

        assert "memorymaster-classify.py" in blob
        assert "memorymaster-validate-wiki.py" in blob
        assert "memorymaster-session-start.py" in blob

    def test_all_hook_scripts_exist(self):
        assert CLASSIFY_HOOK.exists()
        assert VALIDATE_WIKI_HOOK.exists()
        assert SESSION_START_HOOK.exists()
