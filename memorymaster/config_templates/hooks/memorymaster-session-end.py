"""Quiet default session-end distillation with durable cursors and budgets."""

import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = "__MEMORYMASTER_PROJECT_ROOT__"
DB_PATH = os.path.join(PROJECT_ROOT, "memorymaster.db")
sys.path.insert(0, PROJECT_ROOT)


def _enabled(name, default=True):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def main():
    if not _enabled("MEMORYMASTER_SESSION_END_CAPTURE", True):
        return
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}
    transcript_path = data.get("transcript_path", "")
    session_id = str(data.get("session_id") or "unknown")
    cwd = str(data.get("cwd") or PROJECT_ROOT)
    try:
        from memorymaster.core.capture_control import CaptureLedger, capture_state_path
        from scripts.agent_session_end_ingest import run

        ledger = CaptureLedger(capture_state_path())
        chunk = ledger.read_increment(transcript_path, f"{session_id}:session-end")
        if not chunk.text:
            return
        provider = os.environ.get("MEMORYMASTER_LLM_PROVIDER", "google")
        reservation = ledger.reserve_llm(provider, session_id, "session-end-distill")
        if reservation is None:
            return
        temp_path = ""
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".jsonl", delete=False) as handle:
                handle.write(chunk.text)
                temp_path = handle.name
            run(DB_PATH, temp_path, source_agent="session-end-hook", cwd=cwd)
            ledger.finish_llm(reservation, input_bytes=len(chunk.text.encode("utf-8")), output_bytes=0, outcome="ok")
            ledger.commit_cursor(chunk)
        except Exception:
            ledger.finish_llm(reservation, input_bytes=len(chunk.text.encode("utf-8")), output_bytes=0, outcome="error")
        finally:
            if temp_path:
                Path(temp_path).unlink(missing_ok=True)
    except Exception:
        return


if __name__ == "__main__":
    main()
