from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "memorymaster" / "config_templates" / "hooks" / "memorymaster-session-end.py"


def test_session_end_distills_once_and_cursor_survives_restart(tmp_path: Path) -> None:
    project = tmp_path / "project"
    script_dir = project / "scripts"
    script_dir.mkdir(parents=True)
    (script_dir / "__init__.py").write_text("", encoding="utf-8")
    (script_dir / "agent_session_end_ingest.py").write_text(
        """from pathlib import Path
def run(db_path, transcript_path, *, source_agent, cwd, **kwargs):
    Path(cwd, 'distilled.txt').write_text(Path(transcript_path).read_text(encoding='utf-8'), encoding='utf-8')
    return 1
""",
        encoding="utf-8",
    )
    (project / "sitecustomize.py").write_text(
        "import importlib, sys\n"
        "sys.modules['memorymaster.surfaces.session_end_ingest'] = "
        "importlib.import_module('scripts.agent_session_end_ingest')\n",
        encoding="utf-8",
    )
    hook = tmp_path / "session-end.py"
    hook.write_text(
        TEMPLATE.read_text(encoding="utf-8").replace("__MEMORYMASTER_PROJECT_ROOT__", str(project).replace("\\", "/")),
        encoding="utf-8",
    )
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps({"message": {"role": "assistant", "content": "A durable session learning with sufficient content."}}) + "\n")
    env = os.environ.copy()
    env.update(
        {
            "MEMORYMASTER_CAPTURE_STATE_DB": str(tmp_path / "capture.db"),
            "MEMORYMASTER_LLM_PROVIDER": "fake",
            "PYTHONPATH": os.pathsep.join((str(project), str(ROOT))),
        }
    )
    payload = {"session_id": "session-end", "transcript_path": str(transcript), "cwd": str(project)}

    first = subprocess.run([sys.executable, str(hook)], input=json.dumps(payload), text=True, capture_output=True, env=env)
    assert first.returncode == 0, first.stderr
    assert "durable session learning" in (project / "distilled.txt").read_text().lower()
    marker_mtime = (project / "distilled.txt").stat().st_mtime_ns

    second = subprocess.run([sys.executable, str(hook)], input=json.dumps(payload), text=True, capture_output=True, env=env)
    assert second.returncode == 0, second.stderr
    assert (project / "distilled.txt").stat().st_mtime_ns == marker_mtime


def test_lifecycle_receipt_independent_of_distillation(monkeypatch):
    import importlib.util
    import io

    from memorymaster.profile import project_runtime

    spec = importlib.util.spec_from_file_location("ao3_end_hook", TEMPLATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    recorded = []
    monkeypatch.setenv("MEMORYMASTER_SESSION_END_CAPTURE", "0")
    monkeypatch.setattr(project_runtime, "record_session_end", lambda data: recorded.append(data))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "fixture", "cwd": "explicit"})))
    module.main()
    assert recorded == [{"session_id": "fixture", "cwd": "explicit"}]


def test_incremental_hook_restores_prior_tool_provenance_until_genuine_user(tmp_path, monkeypatch):
    import importlib.util
    import io

    from memorymaster.core.capture_control import CaptureLedger
    from memorymaster.profile import project_runtime
    from memorymaster.surfaces import session_end_ingest

    spec = importlib.util.spec_from_file_location("provenance_end_hook", TEMPLATE)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    state_path = tmp_path / "capture.db"
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_STATE_DB", str(state_path))
    monkeypatch.setenv("MEMORYMASTER_SESSION_END_CAPTURE", "1")
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "fake")
    monkeypatch.setattr(project_runtime, "record_session_end", lambda data: None)
    distilled = []
    monkeypatch.setattr(session_end_ingest, "_distill", lambda text: distilled.append(text) or [])
    monkeypatch.setattr(session_end_ingest, "ingest_learnings", lambda *args, **kwargs: 0)
    transcript = tmp_path / "session.jsonl"
    first = {"role": "assistant", "content": [{"type": "tool_use", "id": "recall", "name": "mcp__memorymaster__query_memory"}]}
    transcript.write_text(json.dumps(first) + "\n", encoding="utf-8")
    payload = {"session_id": "fixture", "cwd": str(tmp_path), "transcript_path": str(transcript)}

    def invoke():
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        hook.main()

    invoke()
    assert not distilled
    assert CaptureLedger(state_path).read_increment(transcript, "fixture:session-end").text == ""
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"role": "assistant", "content": "Unmarked recalled echo should never be distilled into a new claim."}) + "\n")
    invoke()
    assert not distilled, "The second invocation forgot the committed memory-tool provenance"
    with transcript.open("a", encoding="utf-8") as handle:
        for role, text in [("user", "Make a fresh authored decision for this database."),
                           ("assistant", "Use SQLite transactions for the newly authored database design.")]:
            handle.write(json.dumps({"role": role, "content": text}) + "\n")
    invoke()
    assert len(distilled) == 1
    assert "SQLite transactions" in distilled[0]
    assert "Unmarked recalled echo" not in distilled[0]


@pytest.mark.parametrize("capture_enabled", [False, True])
def test_lifecycle_receipt_failure_reports_sanitized_cause_and_continues_capture(tmp_path, monkeypatch, capsys, capture_enabled):
    import importlib.util
    import io

    from memorymaster.profile import project_runtime
    from memorymaster.surfaces import session_end_ingest

    spec = importlib.util.spec_from_file_location("observable_end_hook", TEMPLATE)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    monkeypatch.setenv("MEMORYMASTER_SESSION_END_CAPTURE", "1" if capture_enabled else "0")
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_STATE_DB", str(tmp_path / "capture.db"))
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "fake")

    def fail_receipt(data):
        if not capture_enabled:
            raise RuntimeError("fixture failure")
        try:
            raise OSError("private underlying cause")
        except OSError as exc:
            raise RuntimeError("fixture failure") from exc

    monkeypatch.setattr(project_runtime, "record_session_end", fail_receipt)
    captures = []
    monkeypatch.setattr(session_end_ingest, "run", lambda *args, **kwargs: captures.append(kwargs) or 0)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps({"role": "assistant", "content": "A separately authorized authored learning about SQLite transactions."}) + "\n", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "private-session-identifier", "cwd": str(tmp_path), "transcript_path": str(transcript)})))
    hook.main()
    diagnostic = capsys.readouterr().err
    assert "type=RuntimeError" in diagnostic
    assert f"cause={'OSError' if capture_enabled else 'RuntimeError'}" in diagnostic
    assert "fixture failure" not in diagnostic and "private" not in diagnostic
    assert str(tmp_path) not in diagnostic
    assert len(captures) == int(capture_enabled)


def test_lifecycle_receipt_unexpected_programming_error_is_not_swallowed(monkeypatch):
    import importlib.util
    import io

    from memorymaster.profile import project_runtime

    spec = importlib.util.spec_from_file_location("unexpected_end_hook", TEMPLATE)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    monkeypatch.setattr(project_runtime, "record_session_end", lambda data: (_ for _ in ()).throw(TypeError("programming error")))
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    with pytest.raises(TypeError):
        hook.main()
