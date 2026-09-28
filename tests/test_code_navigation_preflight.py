"""Disposable-repository checks for stale and wrong-worktree navigation."""

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

SPEC = importlib.util.spec_from_file_location(
    "code_navigation_preflight", Path(__file__).resolve().parents[1] / "scripts/check_code_navigation.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "-q")
    (tmp_path / "sample.py").write_text("VALUE = 1\n")
    git("add", "sample.py")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    (tmp_path / ".gitnexus").mkdir()
    meta = {"repoPath": str(tmp_path), "lastCommit": git("rev-parse", "HEAD"), "stats": {"embeddings": 7}}
    (tmp_path / ".gitnexus/meta.json").write_text(json.dumps(meta))
    return tmp_path, meta


def test_matching_head_requires_clean_live_source(repo):
    root, _ = repo
    assert MODULE.inspect_index(root)["status"] == "PASS"
    (root / "sample.py").write_text("VALUE = 2\n")
    result = MODULE.inspect_index(root)
    assert "SOURCE_DIRTY_VERIFY_LIVE" in result["issues"]
    assert result["changed_source"] == [{"status": " M", "path": "sample.py"}]


def test_sibling_checkout_and_old_head_fail_even_with_valid_index(repo):
    root, meta = repo
    meta.update(repoPath=str(root.parent / "sibling"), lastCommit="0" * 40)
    (root / ".gitnexus/meta.json").write_text(json.dumps(meta))
    result = MODULE.inspect_index(root)
    assert set(result["issues"]) == {"WRONG_CHECKOUT", "STALE_HEAD"}
    assert result["embeddings"] == 7


def test_untracked_code_is_visible_but_upstream_clones_are_excluded(repo):
    root, _ = repo
    (root / "new.py").write_text("VALUE = 3\n")
    (root / "repos").mkdir()
    (root / "repos/upstream.py").write_text("VALUE = 4\n")
    result = MODULE.inspect_index(root)
    assert result["changed_source"] == [{"status": "??", "path": "new.py"}]


def test_missing_and_invalid_index_never_pass(repo):
    root, _ = repo
    path = root / ".gitnexus/meta.json"
    path.write_text("not-json")
    assert MODULE.inspect_index(root)["issues"] == ["INVALID_METADATA"]
    path.unlink()
    assert MODULE.inspect_index(root)["issues"] == ["INDEX_MISSING"]
