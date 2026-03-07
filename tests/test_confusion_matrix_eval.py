from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "confusion_matrix_eval.py"


def _case_dir() -> Path:
    base = REPO_ROOT / ".tmp_pytest"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="confusion_eval_", dir=str(base)))


def test_confusion_matrix_eval_writes_macro_and_csv() -> None:
    tmp = _case_dir()
    in_path = tmp / "in.jsonl"
    out_json = tmp / "out.json"
    out_csv = tmp / "out.csv"
    in_path.write_text(
        "\n".join(
            [
                '{"expected_status":"stale","predicted_status":"stale"}',
                '{"expected_status":"stale","predicted_status":"active"}',
                '{"expected_status":"conflicted","predicted_status":"conflicted"}',
                '{"expected_status":"active","predicted_status":"active"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--input-jsonl",
            str(in_path),
            "--out-json",
            str(out_json),
            "--out-csv",
            str(out_csv),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert "macro_metrics" in payload["report"]
    assert out_csv.exists()
    csv_text = out_csv.read_text(encoding="utf-8")
    assert "metric,precision,recall,f1,accuracy" in csv_text
    assert "macro," in csv_text


def test_confusion_matrix_eval_strict_macro_threshold() -> None:
    tmp = _case_dir()
    in_path = tmp / "in.jsonl"
    out_json = tmp / "out.json"
    in_path.write_text(
        "\n".join(
            [
                '{"expected_status":"stale","predicted_status":"active"}',
                '{"expected_status":"conflicted","predicted_status":"active"}',
                '{"expected_status":"active","predicted_status":"active"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--input-jsonl",
            str(in_path),
            "--out-json",
            str(out_json),
            "--min-f1-macro",
            "0.60",
            "--strict",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 2
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert any("macro_f1<0.6" in item for item in payload["threshold_failures"])
