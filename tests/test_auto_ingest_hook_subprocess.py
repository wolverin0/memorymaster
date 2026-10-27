"""Run the real auto-ingest Stop hook script end to end (T-1848).

The hook is rendered from the shipped template and executed as a subprocess with a
Claude Code style stdin payload, a tmp HOME, a tmp DB and a tmp capture ledger. Only the
external LLM is stubbed: a loopback HTTP server answers the Ollama-shaped request that
``memorymaster.core.llm_provider`` really sends.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from memorymaster.core.service import MemoryService

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "memorymaster" / "config_templates" / "hooks" / "memorymaster-auto-ingest.py"
LEARNING = "Hook extraction only ingests learnings that pass the sensitivity filter before service ingest"


class _FakeLLM:
    """Loopback Ollama stand-in; records how many times the hook called it."""

    def __init__(self, learnings: list[dict]) -> None:
        self.calls = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.calls += 1
                body = json.dumps({"response": json.dumps(learnings)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_llm():
    servers: list[_FakeLLM] = []

    def make(learnings: list[dict]) -> _FakeLLM:
        servers.append(_FakeLLM(learnings))
        return servers[-1]

    yield make
    for server in servers:
        server.close()


def _run_stop_hook(tmp_path: Path, llm: _FakeLLM, assistant_text: str):
    project_root = tmp_path / "project"
    project_root.mkdir()
    db_path = project_root / "memorymaster.db"
    MemoryService(db_path, workspace_root=project_root).init_db()

    hook = tmp_path / "stop-hook.py"
    hook.write_text(
        TEMPLATE.read_text(encoding="utf-8").replace(
            "__MEMORYMASTER_PROJECT_ROOT__", str(project_root).replace("\\", "/")
        ),
        encoding="utf-8",
    )
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"message": {"role": "user", "content": "what did we decide about the hook"}})
        + "\n"
        + json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": assistant_text}]}})
        + "\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    for key in (
        "MEMORYMASTER_STOP_BLOCKING",
        "MEMORYMASTER_STOP_CAPTURE_VERBATIM",
        "MEMORYMASTER_STOP_RULE_MINING",
        "MEMORYMASTER_WAL_DISCIPLINE",
        "MEMORYMASTER_LLM_FALLBACK_PROVIDER",
        "MEMORYMASTER_JEV_MODE",
        "MEMORYMASTER_JEV_RECALL",
        "MEMORYMASTER_JEV_SESSION",
    ):
        env.pop(key, None)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "USERPROFILE": str(tmp_path / "home"),
            "PYTHONPATH": str(ROOT),
            "MEMORYMASTER_CAPTURE_STATE_DB": str(tmp_path / "capture.db"),
            "MEMORYMASTER_STOP_EXTRACT": "1",
            "MEMORYMASTER_LLM_PROVIDER": "ollama",
            "OLLAMA_URL": llm.url,
        }
    )
    payload = {
        "session_id": "hook-e2e",
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
        timeout=60,
    )
    with sqlite3.connect(db_path) as conn:
        claims = conn.execute("SELECT text, source_agent FROM claims").fetchall()
    conn.close()
    return result, claims


ASSISTANT_TEXT = (
    "The root cause was that the extraction path skipped the sensitivity filter; "
    "we decided to route every learning through the canonical service ingest."
)


def test_auto_ingest_hook_ingests_extracted_learning(tmp_path, fake_llm):
    llm = fake_llm([{"text": LEARNING, "claim_type": "decision", "subject": "stop-hook", "predicate": "ingest"}])

    result, claims = _run_stop_hook(tmp_path, llm, ASSISTANT_TEXT)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"decision": "approve"}
    assert llm.calls == 1
    assert claims == [(LEARNING, "llm-stop-hook")]
    assert "extracted 1 learnings" in result.stderr


def test_auto_ingest_hook_drops_sensitive_learning(tmp_path, fake_llm):
    secret = "AKIA" + "IOSFODNN7EXAMPLE"  # split so secret scanners do not flag this file
    llm = fake_llm([{"text": f"The deploy key is {secret} for the stop hook", "subject": "stop-hook"}])

    result, claims = _run_stop_hook(tmp_path, llm, ASSISTANT_TEXT)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"decision": "approve"}
    assert llm.calls == 1  # the extraction really ran; the filter dropped the result
    assert claims == []


def test_auto_ingest_hook_below_threshold_skips_llm(tmp_path, fake_llm):
    llm = fake_llm([{"text": LEARNING}])

    result, claims = _run_stop_hook(tmp_path, llm, "ok done")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"decision": "approve"}
    assert llm.calls == 0
    assert claims == []
