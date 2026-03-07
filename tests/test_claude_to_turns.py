from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "claude_to_turns.py"


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        payload = line.strip()
        if not payload:
            continue
        rows.append(json.loads(payload))
    return rows


def _run_bridge(input_path: Path, output_path: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        str(SCRIPT_PATH),
        "--input",
        str(input_path),
        "--output",
        str(output_path),
        *extra_args,
    ]
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _case_dir() -> Path:
    base = REPO_ROOT / ".tmp_pytest"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="claude_to_turns_", dir=str(base)))


def test_json_array_messages_to_output_turns() -> None:
    tmp_path = _case_dir()
    input_path = tmp_path / "chat_export.json"
    output_path = tmp_path / "out.jsonl"
    input_payload = [
        {"role": "system", "content": "follow policy"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "tool", "content": "tool trace line"},
        {"role": "user", "content": "next question"},
        {"role": "assistant", "content": "next answer"},
    ]
    input_path.write_text(json.dumps(input_payload), encoding="utf-8")

    result = _run_bridge(input_path, output_path)
    assert result.returncode == 0, result.stderr

    rows = _read_jsonl(output_path)
    assert len(rows) == 2

    first = rows[0]
    second = rows[1]

    assert first["session_id"] == "claude"
    assert first["thread_id"] == "chat_export"
    assert first["turn_id"] == "bridge-0001"
    assert first["user_text"] == "hello"
    assert first["assistant_text"] == "hi there"
    assert first["observations"] == ["follow policy"]

    assert second["turn_id"] == "bridge-0002"
    assert second["user_text"] == "next question"
    assert second["assistant_text"] == "next answer"
    assert second["observations"] == ["tool trace line"]


def test_json_object_with_messages_blocks_to_output_turns() -> None:
    tmp_path = _case_dir()
    input_path = tmp_path / "blocks.json"
    output_path = tmp_path / "out.jsonl"
    input_payload = {
        "messages": [
            {"role": "system", "content": [{"type": "text", "text": "system note"}]},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "image", "url": "https://example.com/i.png"},
                    {"type": "text", "text": "world"},
                ],
            },
            {"role": "assistant", "content": "response"},
        ]
    }
    input_path.write_text(json.dumps(input_payload), encoding="utf-8")

    result = _run_bridge(input_path, output_path, "--session-id", "sess-x", "--thread-id", "thread-x")
    assert result.returncode == 0, result.stderr
    assert "input_rows=1" in result.stdout
    assert "input_messages=3" in result.stdout
    assert "output_turns=1" in result.stdout

    rows = _read_jsonl(output_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "sess-x"
    assert row["thread_id"] == "thread-x"
    assert row["turn_id"] == "bridge-0001"
    assert row["user_text"] == "hello\nworld"
    assert row["assistant_text"] == "response"
    assert row["observations"] == ["system note"]


def test_jsonl_mixed_rows_passthrough_and_normalized() -> None:
    tmp_path = _case_dir()
    input_path = tmp_path / "mixed.jsonl"
    output_path = tmp_path / "out.jsonl"
    input_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "session_id": "s1",
                        "thread_id": "t1",
                        "turn_id": "explicit-1",
                        "user_text": "explicit user",
                        "assistant_text": "explicit assistant",
                        "observations": "existing note",
                        "timestamp": "2026-03-03T12:00:00+00:00",
                    }
                ),
                json.dumps(
                    {
                        "session_id": "s2",
                        "thread_id": "t2",
                        "events": [
                            {"role": "system", "text": "system before user"},
                            {"role": "user", "text": "event user"},
                            {"role": "assistant", "text": "event assistant"},
                            {"role": "tool", "text": "tool tail"},
                        ],
                    }
                ),
                json.dumps(
                    {
                        "messages": [
                            {"role": "system", "content": "system row 3"},
                            {"role": "user", "content": "message user"},
                            {"role": "assistant", "content": "message assistant"},
                        ]
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_bridge(input_path, output_path, "--session-id", "sess-default", "--thread-id", "thread-default")
    assert result.returncode == 0, result.stderr
    assert "output_turns=3" in result.stdout

    rows = _read_jsonl(output_path)
    assert len(rows) == 3

    row1 = rows[0]
    row2 = rows[1]
    row3 = rows[2]

    assert row1["turn_id"] == "explicit-1"
    assert row1["session_id"] == "s1"
    assert row1["thread_id"] == "t1"
    assert row1["observations"] == ["existing note"]

    assert row2["turn_id"] == "bridge-0001"
    assert row2["session_id"] == "s2"
    assert row2["thread_id"] == "t2"
    assert row2["user_text"] == "event user"
    assert row2["assistant_text"] == "event assistant"
    assert row2["observations"] == ["system before user", "tool tail"]

    assert row3["turn_id"] == "bridge-0002"
    assert row3["session_id"] == "sess-default"
    assert row3["thread_id"] == "thread-default"
    assert row3["observations"] == ["system row 3"]
