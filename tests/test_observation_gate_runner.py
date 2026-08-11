from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


RUNNER_PATH = Path(__file__).parents[1] / "scripts" / "run_codex_observation_gate.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("observation_gate_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "log_path": tmp_path / "gate.log",
        "result_path": tmp_path / "gate-result.json",
        "success_marker": tmp_path / "gate-success.json",
        "failure_marker": tmp_path / "gate-failure.md",
    }


def test_zero_child_exit_with_failure_marker_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    paths = _paths(tmp_path)

    def fake_run(*args, **kwargs):
        paths["failure_marker"].write_text("blocked", encoding="utf-8")
        return subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    exit_code = runner.run_gate(
        command=["fake-codex"], prompt="verify", cwd=tmp_path, timeout_seconds=10, **paths
    )

    assert exit_code == runner.FAILURE_MARKER_EXIT
    result = json.loads(paths["result_path"].read_text(encoding="utf-8"))
    assert result["child_exit_code"] == 0
    assert result["gate_exit_code"] == runner.FAILURE_MARKER_EXIT
    assert result["status"] == "failed"


def test_success_requires_explicit_success_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    paths = _paths(tmp_path)
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
    )

    exit_code = runner.run_gate(
        command=["fake-codex"], prompt="verify", cwd=tmp_path, timeout_seconds=10, **paths
    )

    assert exit_code == runner.MISSING_SUCCESS_EXIT


def test_fresh_marker_paths_preserve_prior_failure_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    paths = _paths(tmp_path)
    paths["failure_marker"].write_text("prior failure", encoding="utf-8")
    called = False

    def fake_run(*args, **kwargs):
        nonlocal called
        called = True
        return subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    with pytest.raises(FileExistsError):
        runner.run_gate(
            command=["fake-codex"],
            prompt="verify",
            cwd=tmp_path,
            timeout_seconds=10,
            **paths,
        )

    assert called is False


def test_success_marker_and_clean_child_return_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    paths = _paths(tmp_path)

    def fake_run(*args, **kwargs):
        paths["success_marker"].write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    exit_code = runner.run_gate(
        command=["fake-codex"], prompt="verify", cwd=tmp_path, timeout_seconds=10, **paths
    )

    assert exit_code == 0


def test_child_failure_code_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    paths = _paths(tmp_path)
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 7),
    )

    exit_code = runner.run_gate(
        command=["fake-codex"], prompt="verify", cwd=tmp_path, timeout_seconds=10, **paths
    )

    assert exit_code == 7


def test_timeout_is_recorded_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    paths = _paths(tmp_path)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], timeout=10)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    exit_code = runner.run_gate(
        command=["fake-codex"], prompt="verify", cwd=tmp_path, timeout_seconds=10, **paths
    )

    assert exit_code == runner.TIMEOUT_EXIT
    assert "timed out" in paths["log_path"].read_text(encoding="utf-8")


def test_main_parses_paths_and_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    paths = _paths(tmp_path)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("verify", encoding="utf-8")

    def fake_run(*args, **kwargs):
        paths["success_marker"].write_text("{}", encoding="utf-8")
        assert args[0] == ["fake-codex", "exec"]
        assert kwargs["input"] == "verify"
        return subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    exit_code = runner.main(
        [
            "--prompt-path",
            str(prompt_path),
            "--cwd",
            str(tmp_path),
            "--log-path",
            str(paths["log_path"]),
            "--result-path",
            str(paths["result_path"]),
            "--success-marker",
            str(paths["success_marker"]),
            "--failure-marker",
            str(paths["failure_marker"]),
            "--",
            "fake-codex",
            "exec",
        ]
    )

    assert exit_code == 0


def test_main_rejects_missing_child_command(tmp_path: Path) -> None:
    runner = _load_runner()
    paths = _paths(tmp_path)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("verify", encoding="utf-8")

    with pytest.raises(SystemExit):
        runner.main(
            [
                "--prompt-path",
                str(prompt_path),
                "--cwd",
                str(tmp_path),
                "--log-path",
                str(paths["log_path"]),
                "--result-path",
                str(paths["result_path"]),
                "--success-marker",
                str(paths["success_marker"]),
                "--failure-marker",
                str(paths["failure_marker"]),
            ]
        )
