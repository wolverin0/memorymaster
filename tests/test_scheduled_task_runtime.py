from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

from memorymaster.surfaces.scheduled_task import _run_dream


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
