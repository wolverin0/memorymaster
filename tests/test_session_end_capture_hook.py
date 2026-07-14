from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "memorymaster" / "config_templates" / "hooks" / "memorymaster-session-end.py"


def test_session_end_distills_once_and_cursor_survives_restart(tmp_path: Path) -> None:
    project = tmp_path / "project"
    script_dir = project / "scripts"
    script_dir.mkdir(parents=True)
    (script_dir / "__init__.py").write_text("", encoding="utf-8")
    (script_dir / "agent_session_end_ingest.py").write_text(
        """from pathlib import Path
def run(db_path, transcript_path, *, source_agent, cwd):
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
