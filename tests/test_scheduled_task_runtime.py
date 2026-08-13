from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

from memorymaster.surfaces import scheduled_task
from memorymaster.surfaces.scheduled_task import _parser, _run_dream


def test_scheduled_dream_queues_due_capture_work_before_processing(
    tmp_path: Path, monkeypatch,
) -> None:
    calls: list[tuple[str, object]] = []

    def fake_improve(**kwargs):
        calls.append(("improve", kwargs))
        return SimpleNamespace(to_dict=lambda: {"queued": {"extract_graph": 1}})

    def fake_capture(service, *, limit):
        calls.append(("capture", limit))
        return {"completed": 1}

    def fake_dream(db, workspace, *, apply_candidates):
        calls.append(("dream", apply_candidates))
        return {"ok": True, "errors": 0}

    monkeypatch.setattr("memorymaster.public.v1.improve", fake_improve)
    monkeypatch.setattr("memorymaster.capture.worker.run_capture_worker", fake_capture)
    monkeypatch.setattr("memorymaster.dreaming.worker.run_dream", fake_dream)

    workspace = tmp_path / "memorymaster"
    workspace.mkdir()
    args = Namespace(
        db=str(tmp_path / "scheduled.db"),
        workspace=str(workspace),
        apply_candidates=True,
    )

    assert _run_dream(args) == 0
    assert [name for name, _ in calls] == ["improve", "capture", "dream"]
    improve_kwargs = calls[0][1]
    assert isinstance(improve_kwargs, dict)
    assert improve_kwargs == {
        "db": args.db,
        "workspace": args.workspace,
        "max_items": 25,
        "source_agent": "memorymaster-dreaming",
        "platform": "scheduled",
    }


def test_scheduled_dream_binds_task_provider_contract_over_stale_environment(
    tmp_path: Path, monkeypatch,
) -> None:
    observed: list[tuple[str | None, ...]] = []
    monkeypatch.setenv("MEMORYMASTER_DREAM_EXTRACT_PROVIDER", "opencode")
    monkeypatch.setenv("MEMORYMASTER_DREAM_EXTRACT_MODEL", "openai/gpt-5.6-terra")
    monkeypatch.setenv("MEMORYMASTER_DREAM_CONSOLIDATE_MODEL", "openai/gpt-5.6-luna")
    monkeypatch.setenv("MEMORYMASTER_DREAM_EXTRACT_VARIANT", "medium")
    monkeypatch.setenv("MEMORYMASTER_DREAM_CONSOLIDATE_VARIANT", "low")

    def record_environment(*_args, **_kwargs):
        import os

        observed.append(
            (
                os.environ.get("MEMORYMASTER_DREAM_EXTRACT_PROVIDER"),
                os.environ.get("MEMORYMASTER_DREAM_EXTRACT_MODEL"),
                os.environ.get("MEMORYMASTER_DREAM_CONSOLIDATE_MODEL"),
                os.environ.get("MEMORYMASTER_DREAM_EXTRACT_VARIANT"),
                os.environ.get("MEMORYMASTER_DREAM_CONSOLIDATE_VARIANT"),
            )
        )
        return {"errors": 0}

    monkeypatch.setattr("memorymaster.public.v1.improve", lambda **_kwargs: SimpleNamespace(to_dict=dict))
    monkeypatch.setattr("memorymaster.capture.worker.run_capture_worker", record_environment)
    monkeypatch.setattr(
        "memorymaster.dreaming.worker.run_dream",
        lambda *_args, **_kwargs: {"ok": True, "errors": 0},
    )
    workspace = tmp_path / "memorymaster"
    workspace.mkdir()
    args = _parser().parse_args(
        [
            "dream", "--db", str(tmp_path / "scheduled.db"),
            "--workspace", str(workspace),
            "--extract-provider", "gemini",
            "--extract-model", "gemini-3.5-flash",
            "--consolidate-model", "zai-coding-plan/glm-5.2",
            "--clear-provider-variants",
        ]
    )

    assert _run_dream(args) == 0
    assert observed == [
        ("gemini", "gemini-3.5-flash", "zai-coding-plan/glm-5.2", None, None)
    ]


def test_scheduled_dream_fails_when_capture_provider_errors(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        "memorymaster.public.v1.improve",
        lambda **_kwargs: SimpleNamespace(to_dict=dict),
    )
    monkeypatch.setattr(
        "memorymaster.capture.worker.run_capture_worker",
        lambda *_args, **_kwargs: SimpleNamespace(errors=1),
    )
    monkeypatch.setattr(
        "memorymaster.dreaming.worker.run_dream",
        lambda *_args, **_kwargs: {"ok": True, "errors": 0},
    )
    workspace = tmp_path / "memorymaster"
    workspace.mkdir()
    args = _parser().parse_args(
        ["dream", "--db", str(tmp_path / "scheduled.db"), "--workspace", str(workspace)]
    )

    assert _run_dream(args) == 1


def test_scheduled_dream_runs_enabled_compiled_profile(
    tmp_path: Path, monkeypatch,
) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setenv("MEMORYMASTER_COMPILED_PROFILE", "1")
    monkeypatch.setattr(
        "memorymaster.public.v1.improve",
        lambda **_kwargs: SimpleNamespace(to_dict=dict),
    )
    monkeypatch.setattr(
        "memorymaster.capture.worker.run_capture_worker",
        lambda *_args, **_kwargs: {"errors": 0},
    )
    monkeypatch.setattr(
        "memorymaster.dreaming.worker.run_dream",
        lambda *_args, **_kwargs: {"ok": True, "errors": 0},
    )

    def fake_profile(db, **kwargs):
        calls.append((db, kwargs))
        return {"ok": True, "status": "mapping"}

    monkeypatch.setattr("memorymaster.profile.engine.run_compiled_profile", fake_profile)
    workspace = tmp_path / "memorymaster"
    workspace.mkdir()
    args = Namespace(
        db=str(tmp_path / "scheduled.db"),
        workspace=str(workspace),
        apply_candidates=True,
    )

    assert _run_dream(args) == 0
    assert calls == [(args.db, {})]


def test_scheduled_dream_fails_closed_on_enabled_profile_error(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("MEMORYMASTER_COMPILED_PROFILE", "1")
    monkeypatch.setattr(
        "memorymaster.public.v1.improve",
        lambda **_kwargs: SimpleNamespace(to_dict=dict),
    )
    monkeypatch.setattr(
        "memorymaster.capture.worker.run_capture_worker",
        lambda *_args, **_kwargs: {"errors": 0},
    )
    monkeypatch.setattr(
        "memorymaster.dreaming.worker.run_dream",
        lambda *_args, **_kwargs: {"ok": True, "errors": 0},
    )
    monkeypatch.setattr(
        "memorymaster.profile.engine.run_compiled_profile",
        lambda *_args, **_kwargs: {"ok": False, "status": "mapping"},
    )
    workspace = tmp_path / "memorymaster"
    workspace.mkdir()
    args = Namespace(
        db=str(tmp_path / "scheduled.db"),
        workspace=str(workspace),
        apply_candidates=True,
    )

    assert _run_dream(args) == 1


def test_scheduled_main_logs_bound_dream_execution(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "dream.log"
    seen: list[str] = []
    monkeypatch.setattr(scheduled_task, "_log_path", lambda _mode: log_path)
    monkeypatch.setattr(
        scheduled_task,
        "_run_dream",
        lambda args: seen.append(args.extract_provider) or 0,
    )

    result = scheduled_task.main(
        [
            "dream", "--db", str(tmp_path / "scheduled.db"),
            "--workspace", str(tmp_path), "--extract-provider", "gemini",
        ]
    )

    assert result == 0
    assert seen == ["gemini"]
    assert "dream start" in log_path.read_text(encoding="utf-8")
