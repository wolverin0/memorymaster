"""Red contract for the Stop hook's default quiet capture policy."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "memorymaster" / "config_templates" / "hooks" / "memorymaster-auto-ingest.py"


def _render_hook(project_root: Path, destination: Path) -> Path:
    rendered = TEMPLATE.read_text(encoding="utf-8").replace(
        "__MEMORYMASTER_PROJECT_ROOT__",
        str(project_root).replace("\\", "/"),
    )
    destination.write_text(rendered, encoding="utf-8")
    return destination


def _write_transcript(path: Path) -> Path:
    records = [
        json.dumps(
            {
                "message": {
                    "role": "user",
                    "content": f"Human message {index} has enough content for capture policy testing.",
                }
            }
        )
        for index in range(15)
    ]
    path.write_text("\n".join(records) + "\n", encoding="utf-8")
    return path


def test_default_stop_hook_is_quiet_and_nonblocking(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    hook = _render_hook(project_root, tmp_path / "stop-hook.py")
    transcript = _write_transcript(tmp_path / "session.jsonl")
    spool_root = tmp_path / "spool"
    env = os.environ.copy()
    for key in (
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "MEMORYMASTER_STOP_BLOCKING",
        "MEMORYMASTER_STOP_CAPTURE_VERBATIM",
        "MEMORYMASTER_STOP_EXTRACT",
        "MEMORYMASTER_STOP_RULE_MINING",
    ):
        env.pop(key, None)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "USERPROFILE": str(tmp_path / "home"),
            "MEMORYMASTER_SPOOL_DIR": str(spool_root),
            "MEMORYMASTER_WAL_DISCIPLINE": "1",
            "PYTHONPATH": str(ROOT),
        }
    )
    payload = {
        "session_id": "default-policy",
        "transcript_path": str(transcript),
        "cwd": str(project_root),
        "stop_hook_active": False,
    }

    result = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    envelopes = [
        line
        for path in spool_root.rglob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"decision": "approve"}
    assert envelopes == []


def test_blocking_requires_explicit_maximum_capture_flag(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    hook = _render_hook(project_root, tmp_path / "stop-hook.py")
    transcript = _write_transcript(tmp_path / "session.jsonl")
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "USERPROFILE": str(tmp_path / "home"),
            "MEMORYMASTER_STOP_BLOCKING": "1",
            "PYTHONPATH": str(ROOT),
        }
    )
    payload = {
        "session_id": "maximum-policy",
        "transcript_path": str(transcript),
        "cwd": str(project_root),
        "stop_hook_active": False,
    }

    result = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block"


def test_verbatim_capture_only_spools_appended_turns(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    hook = _render_hook(project_root, tmp_path / "stop-hook.py")
    transcript = _write_transcript(tmp_path / "session.jsonl")
    spool_root = tmp_path / "spool"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "USERPROFILE": str(tmp_path / "home"),
            "MEMORYMASTER_CAPTURE_STATE_DB": str(tmp_path / "capture.db"),
            "MEMORYMASTER_SPOOL_DIR": str(spool_root),
            "MEMORYMASTER_WAL_DISCIPLINE": "1",
            "MEMORYMASTER_STOP_CAPTURE_VERBATIM": "1",
            "PYTHONPATH": str(ROOT),
        }
    )
    payload = {
        "session_id": "cursor-policy",
        "transcript_path": str(transcript),
        "cwd": str(project_root),
        "stop_hook_active": False,
    }

    def run_hook() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    assert run_hook().returncode == 0
    first = [line for path in spool_root.rglob("*.jsonl") for line in path.read_text().splitlines()]
    assert len(first) == 15

    assert run_hook().returncode == 0
    second = [line for path in spool_root.rglob("*.jsonl") for line in path.read_text().splitlines()]
    assert second == first

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"message": {"role": "assistant", "content": "A newly appended assistant turn with enough detail."}}) + "\n")
    assert run_hook().returncode == 0
    third = [line for path in spool_root.rglob("*.jsonl") for line in path.read_text().splitlines()]
    assert len(third) == 16


# --- T-2116: the retention gate must not silently stop storing -----------------------------------

def _verbatim_env(tmp_path, **extra):
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "USERPROFILE": str(tmp_path / "home"),
            "MEMORYMASTER_CAPTURE_STATE_DB": str(tmp_path / "capture.db"),
            "MEMORYMASTER_WAL_DISCIPLINE": "0",
            "MEMORYMASTER_STOP_CAPTURE_VERBATIM": "1",
            "PYTHONPATH": str(ROOT),
            **extra,
        }
    )
    return env


def _run_verbatim_hook(tmp_path, project_root, env):
    hook = _render_hook(project_root, tmp_path / "stop-hook.py")
    transcript = _write_transcript(tmp_path / "session.jsonl")
    payload = {"session_id": "gate", "transcript_path": str(transcript), "cwd": str(project_root),
               "stop_hook_active": False}
    return subprocess.run([sys.executable, str(hook)], input=json.dumps(payload), capture_output=True,
                          text=True, env=env, timeout=120)


def _health(tmp_path):
    path = tmp_path / "home" / ".memorymaster" / "hook_state" / "verbatim-health.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def test_overdue_retention_signals_but_never_stops_storing(tmp_path):
    import sqlite3

    from memorymaster.recall.verbatim_store import ensure_verbatim_schema

    project_root = tmp_path / "project"
    project_root.mkdir()
    db = project_root / "memorymaster.db"
    ensure_verbatim_schema(str(db))
    conn = sqlite3.connect(db)  # rows far older than the 30-day age that used to make the gate refuse
    conn.execute("INSERT INTO verbatim_memories (session_id, role, content, timestamp, created_at) "
                 "VALUES ('old', 'user', 'ancient turn', '2026-04-08T18:47:56+00:00', '2026-04-08T18:47:56Z')")
    conn.commit()
    conn.close()

    result = _run_verbatim_hook(tmp_path, project_root, _verbatim_env(tmp_path))

    assert result.returncode == 0
    conn = sqlite3.connect(db)
    stored = conn.execute("SELECT COUNT(*) FROM verbatim_memories WHERE session_id = 'gate'").fetchone()[0]
    conn.close()
    assert stored == 15  # storing was not skipped
    health = _health(tmp_path)
    assert health["retention_due"] is True and health["oldest_age_days"] > 90
    assert health["last_store_at"]


def test_a_failed_store_leaves_a_counter_and_a_log_line(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "memorymaster.db").write_bytes(b"this is not a sqlite database" * 64)

    result = _run_verbatim_hook(tmp_path, project_root, _verbatim_env(tmp_path))

    assert result.returncode == 0  # the Stop hook itself still succeeds
    health = _health(tmp_path)
    assert health["store_skipped_count"] == 1
    assert health["last_skip_reason"].startswith("store-failed:")
    log = (tmp_path / "home" / ".memorymaster" / "hook_state" / "hook.log").read_text()
    assert "verbatim-health" in log and "store-failed" in log
