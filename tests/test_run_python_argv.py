from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_python_argv.py"


def test_runner_preserves_json_and_spaced_arguments(tmp_path: Path) -> None:
    capture = tmp_path / "capture args.py"
    output = tmp_path / "captured args.json"
    arguments_file = tmp_path / "input args.json"
    expected = [
        "--acceptance-criteria",
        '[{"metric_key":"graph_hit_rate_at_5","operator":">=","target":1.0}]',
        "--goal",
        "graph quality with spaces",
    ]
    capture.write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    arguments_file.write_text(json.dumps([str(output), *expected]), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--script",
            str(capture),
            "--arguments",
            str(arguments_file),
        ],
        cwd=ROOT,
        check=False,
    )

    assert completed.returncode == 0
    assert json.loads(output.read_text(encoding="utf-8")) == expected
