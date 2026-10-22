"""Quiet default session-end distillation with durable cursors and budgets."""

import json
import os
import sys

PROJECT_ROOT = "__MEMORYMASTER_PROJECT_ROOT__"
DB_PATH = os.path.join(PROJECT_ROOT, "memorymaster.db")
sys.path.insert(0, PROJECT_ROOT)


def _enabled(name, default=True):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    try:
        from memorymaster.profile.project_runtime import record_session_end

        record_session_end(data)
    except Exception:
        pass  # Lifecycle failure must not suppress independently authorized capture.
    if not _enabled("MEMORYMASTER_SESSION_END_CAPTURE", True):
        return
    transcript_path = data.get("transcript_path", "")
    session_id = str(data.get("session_id") or "unknown")
    cwd = str(data.get("cwd") or PROJECT_ROOT)
    try:
        from memorymaster.core.capture_control import CaptureLedger, capture_state_path
        from memorymaster.surfaces.session_end_ingest import run

        ledger = CaptureLedger(capture_state_path())
        chunk = ledger.read_increment(transcript_path, f"{session_id}:session-end")
        if not chunk.text:
            return
        provider = os.environ.get("MEMORYMASTER_LLM_PROVIDER", "google")
        reservation = ledger.reserve_llm(provider, session_id, "session-end-distill")
        if reservation is None:
            return
        try:
            # Reconstruct structural provenance from the chronological source,
            # but select evidence only within this cursor's complete records.
            run(DB_PATH, transcript_path, source_agent="session-end-hook", cwd=cwd,
                start_offset=chunk.start_offset, end_offset=chunk.end_offset)
            ledger.finish_llm(reservation, input_bytes=len(chunk.text.encode("utf-8")), output_bytes=0, outcome="ok")
            ledger.commit_cursor(chunk)
        except Exception:  # swallow-ok: hook quiet-by-contract; `capture_usage.outcome`
        # es vocabulario de contabilidad ('ok'/'error'), no un campo de diagnostico.
        # Guardar la causa pide una columna nueva en capture_usage, que es otro
        # subsistema. Deuda anotada, no escondida.
            ledger.finish_llm(reservation, input_bytes=len(chunk.text.encode("utf-8")), output_bytes=0, outcome="error")
    except Exception:
        return


if __name__ == "__main__":
    main()
