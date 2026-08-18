"""The stale-branch guard must fail loudly and never block work spuriously.

A guard that fires on a healthy branch is worse than no guard: it trains
everyone to ignore it. These pin both directions — a genuinely stale branch
fails, a current one passes, and an unfetched base is reported without failing
the caller's work over a setup problem.
"""
from __future__ import annotations

import subprocess

import pytest

from scripts.check_branch_freshness import main


def _run(*args: str, cwd) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A repo whose 'origin/main' has moved ahead of a side branch."""
    path = tmp_path / "repo"
    path.mkdir()
    _run("init", "-q", "-b", "main", cwd=path)
    _run("config", "user.email", "t@example.com", cwd=path)
    _run("config", "user.name", "test", cwd=path)
    (path / "f.txt").write_text("base\n", encoding="utf-8")
    _run("add", "-A", cwd=path)
    _run("commit", "-qm", "base", cwd=path)
    _run("branch", "side", cwd=path)
    # 'origin/main' is a local ref here; the script only ever reads it.
    _run("update-ref", "refs/remotes/origin/main", "main", cwd=path)
    monkeypatch.chdir(path)
    return path


def _advance_main(repo, commits: int) -> None:
    for i in range(commits):
        (repo / "f.txt").write_text(f"change {i}\n", encoding="utf-8")
        _run("add", "-A", cwd=repo)
        _run("commit", "-qm", f"c{i}", cwd=repo)
    _run("update-ref", "refs/remotes/origin/main", "main", cwd=repo)


def test_current_branch_passes(repo, capsys):
    assert main([]) == 0
    assert "ok" in capsys.readouterr().out


def test_slightly_behind_branch_still_passes(repo, capsys):
    """Ordinary drift must not block work, or the guard gets ignored."""
    _advance_main(repo, 3)
    _run("checkout", "-q", "side", cwd=repo)
    assert main([]) == 0


def test_far_behind_branch_fails(repo, capsys):
    _advance_main(repo, 6)
    _run("checkout", "-q", "side", cwd=repo)
    assert main(["--max-behind", "4"]) == 1
    err = capsys.readouterr().err
    assert "6 commits behind" in err
    assert "worktree" in err, "the message must say how to get unstuck"


def test_threshold_is_a_limit_not_a_target(repo):
    """Exactly at the limit is still fine; only past it fails."""
    _advance_main(repo, 4)
    _run("checkout", "-q", "side", cwd=repo)
    assert main(["--max-behind", "4"]) == 0
    assert main(["--max-behind", "3"]) == 1


def test_unknown_base_reports_but_does_not_fail(repo, capsys):
    """A missing ref is a setup problem, not a reason to block a commit."""
    assert main(["--base", "origin/does-not-exist"]) == 0
    assert "could not compare" in capsys.readouterr().err
