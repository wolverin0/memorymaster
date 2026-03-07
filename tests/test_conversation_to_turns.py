from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "conversation_to_turns.py"


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        payload = line.strip()
        if not payload:
            continue
        rows.append(json.loads(payload))
    return rows


def _run_converter(input_path: Path, output_path: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
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
    return Path(tempfile.mkdtemp(prefix="conversation_to_turns_", dir=str(base)))


def test_openai_mapping_export_to_turns() -> None:
    tmp_path = _case_dir()
    input_path = tmp_path / "openai_export.json"
    output_path = tmp_path / "out.jsonl"
    input_payload = {
        "conversations": [
            {
                "session_id": "openai-session",
                "thread_id": "openai-thread",
                "mapping": {
                    "n4": {
                        "id": "n4",
                        "parent": "n3",
                        "children": ["n5"],
                        "message": {
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["second question"]},
                            "create_time": 4,
                        },
                    },
                    "n1": {
                        "id": "n1",
                        "parent": None,
                        "children": ["n2"],
                        "message": {
                            "author": {"role": "system"},
                            "content": {"content_type": "text", "parts": ["policy note"]},
                            "create_time": 1,
                        },
                    },
                    "n3": {
                        "id": "n3",
                        "parent": "n2",
                        "children": ["n4"],
                        "message": {
                            "author": {"role": "assistant"},
                            "content": {"content_type": "text", "parts": ["first answer"]},
                            "create_time": 3,
                        },
                    },
                    "n5": {
                        "id": "n5",
                        "parent": "n4",
                        "children": [],
                        "message": {
                            "author": {"role": "assistant"},
                            "content": {"content_type": "text", "parts": ["second answer"]},
                            "create_time": 5,
                        },
                    },
                    "n2": {
                        "id": "n2",
                        "parent": "n1",
                        "children": ["n3"],
                        "message": {
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["first question"]},
                            "create_time": 2,
                        },
                    },
                },
                "current_node": "n5",
            }
        ]
    }
    input_path.write_text(json.dumps(input_payload), encoding="utf-8")

    result = _run_converter(input_path, output_path)
    assert result.returncode == 0, result.stderr
    assert "input_rows=1" in result.stdout
    assert "output_turns=2" in result.stdout

    rows = _read_jsonl(output_path)
    assert len(rows) == 2

    first = rows[0]
    second = rows[1]
    assert first["session_id"] == "openai-session"
    assert first["thread_id"] == "openai-thread"
    assert first["turn_id"] == "bridge-0001"
    assert first["user_text"] == "first question"
    assert first["assistant_text"] == "first answer"
    assert first["observations"] == ["policy note"]

    assert second["turn_id"] == "bridge-0002"
    assert second["user_text"] == "second question"
    assert second["assistant_text"] == "second answer"
    assert second["observations"] == []


def test_claude_like_messages_array_to_turns() -> None:
    tmp_path = _case_dir()
    input_path = tmp_path / "claude_export.json"
    output_path = tmp_path / "out.jsonl"
    input_payload = [
        {"role": "system", "content": "follow safety policy"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "tool", "content": "tool diagnostic"},
        {"role": "user", "content": "next"},
        {"role": "assistant", "content": "answer"},
    ]
    input_path.write_text(json.dumps(input_payload), encoding="utf-8")

    result = _run_converter(input_path, output_path)
    assert result.returncode == 0, result.stderr

    rows = _read_jsonl(output_path)
    assert len(rows) == 2

    first = rows[0]
    second = rows[1]
    assert first["session_id"] == "import"
    assert first["thread_id"] == "claude_export"
    assert first["turn_id"] == "bridge-0001"
    assert first["observations"] == ["follow safety policy"]
    assert first["user_text"] == "hello"
    assert first["assistant_text"] == "hi"

    assert second["turn_id"] == "bridge-0002"
    assert second["observations"] == ["tool diagnostic"]
    assert second["user_text"] == "next"
    assert second["assistant_text"] == "answer"


def test_gemini_jsonl_and_turn_ids_are_deterministic() -> None:
    tmp_path = _case_dir()
    input_path = tmp_path / "mixed.jsonl"
    output_a = tmp_path / "out_a.jsonl"
    output_b = tmp_path / "out_b.jsonl"
    input_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "session_id": "s1",
                        "thread_id": "t1",
                        "turn_id": "explicit-1",
                        "user_text": "existing user",
                        "assistant_text": "existing assistant",
                        "observations": "existing note",
                    }
                ),
                json.dumps(
                    {
                        "session_id": "s2",
                        "thread_id": "t2",
                        "contents": [
                            {"role": "system", "parts": [{"text": "context note"}]},
                            {"role": "user", "parts": [{"text": "gemini question 1"}]},
                            {"role": "model", "parts": [{"text": "gemini answer 1"}]},
                            {"role": "user", "parts": [{"text": "gemini question 2"}]},
                            {"role": "model", "parts": [{"text": "gemini answer 2"}]},
                        ],
                    }
                ),
                json.dumps({"role": "user", "parts": [{"text": "orphan user"}]}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result_a = _run_converter(input_path, output_a, "--session-id", "sess-default", "--thread-id", "thread-default")
    result_b = _run_converter(input_path, output_b, "--session-id", "sess-default", "--thread-id", "thread-default")
    assert result_a.returncode == 0, result_a.stderr
    assert result_b.returncode == 0, result_b.stderr

    rows_a = _read_jsonl(output_a)
    rows_b = _read_jsonl(output_b)
    assert rows_a == rows_b
    assert [row["turn_id"] for row in rows_a] == ["explicit-1", "bridge-0001", "bridge-0002", "bridge-0003"]

    assert rows_a[0]["observations"] == ["existing note"]
    assert rows_a[1]["session_id"] == "s2"
    assert rows_a[1]["thread_id"] == "t2"
    assert rows_a[1]["observations"] == ["context note"]
    assert rows_a[1]["user_text"] == "gemini question 1"
    assert rows_a[1]["assistant_text"] == "gemini answer 1"
    assert rows_a[2]["user_text"] == "gemini question 2"
    assert rows_a[2]["assistant_text"] == "gemini answer 2"
    assert rows_a[3]["session_id"] == "sess-default"
    assert rows_a[3]["thread_id"] == "thread-default"
    assert rows_a[3]["user_text"] == "orphan user"
    assert rows_a[3]["assistant_text"] == ""
