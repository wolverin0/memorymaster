"""Tests for memorymaster.scheduler — daemon loop, git trigger, and helpers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch


from memorymaster.scheduler import get_git_head, run_daemon, utc_now


class TestUtcNow:
    def test_returns_iso_string(self):
        result = utc_now()
        assert "T" in result
        assert "+" in result or "Z" in result

    def test_no_microseconds(self):
        result = utc_now()
        # ISO without microseconds has no '.' before timezone
        assert "." not in result.split("+")[0]


class TestGetGitHead:
    def test_returns_valid_sha(self, tmp_path):
        """When run inside a real git repo, returns a 40-char hex SHA."""
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "init"], capture_output=True, check=True)
        sha = get_git_head(tmp_path)
        assert sha is not None
        assert len(sha) == 40
        assert all(c in "0123456789abcdef" for c in sha)

    def test_returns_none_for_non_git_dir(self, tmp_path):
        assert get_git_head(tmp_path) is None

    def test_returns_none_on_timeout(self, tmp_path):
        with patch("memorymaster.scheduler.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 10)):
            assert get_git_head(tmp_path) is None

    def test_returns_none_on_os_error(self, tmp_path):
        with patch("memorymaster.scheduler.subprocess.run", side_effect=OSError("no git")):
            assert get_git_head(tmp_path) is None

    def test_returns_none_for_invalid_output(self, tmp_path):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "not-a-sha\n"
        with patch("memorymaster.scheduler.subprocess.run", return_value=mock_proc):
            assert get_git_head(tmp_path) is None

    def test_returns_none_for_empty_output(self, tmp_path):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = ""
        with patch("memorymaster.scheduler.subprocess.run", return_value=mock_proc):
            assert get_git_head(tmp_path) is None


class TestRunDaemon:
    def _mock_service(self):
        svc = MagicMock()
        svc.workspace_root = Path(".")
        svc.run_cycle.return_value = {"extracted": 0, "validated": 0}
        return svc

    def test_runs_exact_cycles(self):
        svc = self._mock_service()
        result = run_daemon(svc, interval_seconds=0, max_cycles=3)
        assert result == {"cycles": 3}
        assert svc.run_cycle.call_count == 3

    def test_compact_every_triggers_compactor(self):
        svc = self._mock_service()
        run_daemon(svc, interval_seconds=0, max_cycles=4, compact_every=2)
        calls = svc.run_cycle.call_args_list
        # Cycle 1: no compact, Cycle 2: compact, Cycle 3: no, Cycle 4: compact
        assert calls[0].kwargs.get("run_compactor") is False or calls[0][1].get("run_compactor") is False
        assert calls[1].kwargs.get("run_compactor") is True or calls[1][1].get("run_compactor") is True

    def test_zero_max_cycles_returns_immediately(self):
        svc = self._mock_service()
        result = run_daemon(svc, max_cycles=0)
        assert result == {"cycles": 0}
        svc.run_cycle.assert_not_called()

    def test_git_trigger_detects_new_commit(self, capsys):
        """First cycle fires on timer (next_due starts at now), second fires on commit."""
        svc = self._mock_service()
        call_count = 0
        heads = ["a" * 40, "a" * 40, "b" * 40]

        def fake_git_head(_):
            nonlocal call_count
            idx = min(call_count, len(heads) - 1)
            call_count += 1
            return heads[idx]

        with patch("memorymaster.scheduler.get_git_head", side_effect=fake_git_head):
            result = run_daemon(
                svc,
                interval_seconds=9999,  # timer won't fire after first
                max_cycles=2,
                git_trigger=True,
                git_check_seconds=0,
            )
        assert result["cycles"] == 2
        captured = capsys.readouterr()
        lines = [l for l in captured.out.strip().split("\n") if l]
        events = [json.loads(l) for l in lines]
        # Second cycle should be commit-triggered
        assert events[1]["trigger"] == "commit"

    def test_git_unavailable_warning(self, capsys):
        svc = self._mock_service()

        with patch("memorymaster.scheduler.get_git_head", return_value=None):
            run_daemon(
                svc,
                interval_seconds=0,
                max_cycles=1,
                git_trigger=True,
                git_check_seconds=0,
            )
        captured = capsys.readouterr()
        lines = [l for l in captured.out.strip().split("\n") if l]
        # Should have a git_unavailable event
        events = [json.loads(l) for l in lines]
        assert any(e.get("event") == "git_unavailable" for e in events)

    def test_passes_policy_params(self):
        svc = self._mock_service()
        run_daemon(
            svc,
            interval_seconds=0,
            max_cycles=1,
            policy_mode="strict",
            policy_limit=50,
            min_citations=2,
            min_score=0.75,
        )
        call_kwargs = svc.run_cycle.call_args.kwargs
        assert call_kwargs["policy_mode"] == "strict"
        assert call_kwargs["policy_limit"] == 50
        assert call_kwargs["min_citations"] == 2
        assert call_kwargs["min_score"] == 0.75

    def test_prints_json_output(self, capsys):
        svc = self._mock_service()
        run_daemon(svc, interval_seconds=0, max_cycles=1)
        captured = capsys.readouterr()
        data = json.loads(captured.out.strip())
        assert "cycle" in data
        assert "result" in data
        assert data["cycle"] == 1
