"""JSON-lines logs for the shared MCP server and the scheduled jobs (T-0764).

The pilot bar: every line parseable as JSON, and an alert ("error rate per
tool in 15 min") definable from fields alone: tool, outcome, duration_ms,
scope, surface. Both renderers (structlog, stdlib fallback) emit the same keys.
"""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from memorymaster.core import structured_log as sl

REQUIRED = {"ts", "level", "logger", "component", "event"}


@pytest.fixture(params=["structlog", "stdlib-json"])
def stream(request, monkeypatch):
    if request.param == "stdlib-json":
        monkeypatch.setattr(sl, "_structlog_formatter", lambda component: None)
    buffer = io.StringIO()
    assert sl.configure_structured_logging(buffer, component="test") == request.param
    yield buffer
    sl.reset_structured_logging()


def _lines(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def test_events_are_json_lines_with_fields_as_top_level_keys(stream):
    sl.log_event(logging.getLogger("memorymaster.x"), "mcp_tool", tool="recall", scope="project:a",
                 duration_ms=12.5, outcome="ok")
    [line] = _lines(stream)
    assert REQUIRED <= set(line)
    assert (line["event"], line["tool"], line["scope"], line["outcome"]) == ("mcp_tool", "recall", "project:a", "ok")
    assert line["component"] == "test" and line["level"] == "info" and line["duration_ms"] == 12.5
    assert line["ts"].endswith("Z") or line["ts"].endswith("+00:00")


def test_third_party_stdlib_records_are_json_too(stream):
    logging.getLogger("uvicorn.error").warning("Started server process [%s]", 42)
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("uvicorn.error").exception("handler crashed")
    started, crashed = _lines(stream)
    assert started["event"] == "Started server process [42]" and started["logger"] == "uvicorn.error"
    assert crashed["level"] == "error" and "ValueError: boom" in crashed["exception"]


def test_timed_event_reports_duration_and_outcome(stream):
    log = logging.getLogger("memorymaster.jobs")
    with sl.timed_event(log, "job_finish", job="dream") as extra:
        extra["exit_code"] = 0
    with sl.timed_event(log, "job_finish", job="dream") as extra:
        extra["outcome"] = "failed"
    with pytest.raises(RuntimeError), sl.timed_event(log, "job_finish", job="dream"):
        raise RuntimeError("x")
    ok, failed, error = _lines(stream)
    assert ok["outcome"] == "ok" and ok["exit_code"] == 0 and isinstance(ok["duration_ms"], float)
    assert failed["outcome"] == "failed"
    assert error["outcome"] == "error" and error["error_type"] == "RuntimeError" and error["level"] == "warning"


def test_logging_never_raises_into_the_caller(stream, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk gone")

    log = logging.getLogger("memorymaster.x")
    monkeypatch.setattr(log, "log", broken)
    sl.log_event(log, "anything", tool="x")  # must not raise


def test_reconfiguring_replaces_only_our_handler():
    root = logging.getLogger()
    other = logging.NullHandler()
    root.addHandler(other)
    try:
        sl.configure_structured_logging(io.StringIO(), component="a")
        sl.configure_structured_logging(io.StringIO(), component="b")
        ours = [h for h in root.handlers if getattr(h, sl._HANDLER_MARK, False)]
        assert len(ours) == 1 and other in root.handlers
        sl.reset_structured_logging()
        assert not [h for h in root.handlers if getattr(h, sl._HANDLER_MARK, False)]
    finally:
        root.removeHandler(other)


# --- the shared MCP server: one event per tool call ---------------------------------------


def _guarded(func, action="query"):
    from memorymaster.surfaces import mcp_server

    return mcp_server._authorized_tool_callable(func, mcp_server.McpToolPolicy(action, team_enabled=True))


def test_mcp_tool_calls_emit_one_event_each(stream, monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    workspace = tmp_path / "alpha"

    def recall(query: str = "", workspace: str = ".") -> dict:
        return {"ok": True}

    def ingest_claim(text: str = "", workspace: str = ".") -> dict:
        return {"ok": False, "error": "rate limited", "code": "RATE_LIMITED"}

    _guarded(recall)(query="q", workspace=str(workspace))
    _guarded(ingest_claim, "ingest")(text="t", workspace=str(workspace))
    with pytest.raises(PermissionError):
        _guarded(recall)(query="q", workspace="${CLAUDE_PROJECT_DIR}")
    ok, failed, refused = [line for line in _lines(stream) if line["event"] == "mcp_tool"]
    assert (ok["tool"], ok["surface"], ok["outcome"], ok["scope"], ok["mode"]) == (
        "recall", "mcp", "ok", "project:alpha", "local-trusted")
    assert (failed["outcome"], failed["error_code"]) == ("failed", "RATE_LIMITED")
    assert (refused["outcome"], refused["error_type"]) == ("error", "PermissionError")
    assert all(isinstance(line["duration_ms"], float) for line in (ok, failed, refused))


def test_stdio_without_configured_logging_emits_nothing(monkeypatch, capsys):
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    sl.reset_structured_logging()
    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.WARNING)
    try:
        assert _guarded(lambda workspace=".": "done")() == "done"
    finally:
        root.setLevel(previous)
    captured = capsys.readouterr()
    assert "mcp_tool" not in captured.out + captured.err


def test_http_server_routes_uvicorn_logs_to_the_json_handler(monkeypatch):
    import asyncio

    import uvicorn

    from memorymaster.surfaces import mcp_http

    seen = {}

    class FakeServer:
        def __init__(self, config):
            seen["log_config"] = config.log_config

        async def serve(self):
            return None

    monkeypatch.setattr(mcp_http.os, "name", "nt")
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(asyncio, "run", lambda coro, **kwargs: coro.close())
    mcp_http._serve(object(), host="127.0.0.1", port=1)
    assert seen["log_config"] is None  # uvicorn must not install its own text formatter


# --- the scheduled jobs: every line of the job log is JSON -----------------------------------


def _run_job(tmp_path: Path, monkeypatch, outcome):
    from memorymaster.surfaces import scheduled_task

    log_path = tmp_path / "dream.log"
    monkeypatch.setattr(scheduled_task, "_log_path", lambda _mode: log_path)
    monkeypatch.setattr(scheduled_task, "_run_dream", outcome)
    code = scheduled_task.main(["dream", "--db", str(tmp_path / "x.db"), "--workspace", str(tmp_path / "beta")])
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return code, lines


def test_job_log_is_all_json_with_a_timed_finish(tmp_path, monkeypatch):
    code, lines = _run_job(tmp_path, monkeypatch, lambda args: 0)
    assert code == 0
    start, finish = lines[0], lines[-1]
    assert (start["event"], start["job"], start["surface"], start["scope"]) == ("job_start", "dream", "job", "project:beta")
    assert (finish["event"], finish["outcome"], finish["exit_code"]) == ("job_finish", "ok", 0)
    assert isinstance(finish["duration_ms"], float) and finish["component"] == "job-dream"


def test_failed_and_crashed_jobs_are_labelled(tmp_path, monkeypatch):
    code, lines = _run_job(tmp_path, monkeypatch, lambda args: 1)
    assert code == 1 and lines[-1]["outcome"] == "failed" and lines[-1]["exit_code"] == 1

    def crash(args):
        raise RuntimeError("provider down")

    crashed_dir = tmp_path / "crashed"
    crashed_dir.mkdir()
    code, lines = _run_job(crashed_dir, monkeypatch, crash)
    finish = [line for line in lines if line["event"] == "job_finish"][-1]
    error = [line for line in lines if line["event"] == "job_error"][-1]
    assert code == 1 and finish["outcome"] == "error" and finish["error_type"] == "RuntimeError"
    assert error["level"] == "error" and "provider down" in error["error"]


def test_job_does_not_leave_its_handler_on_the_root_logger(tmp_path, monkeypatch):
    _run_job(tmp_path, monkeypatch, lambda args: 0)
    assert not [h for h in logging.getLogger().handlers if getattr(h, sl._HANDLER_MARK, False)]


def test_scope_field_tolerates_any_workspace():
    from memorymaster.surfaces import mcp_server

    assert mcp_server._scope_for_log({"workspace": "."}) is None
    assert mcp_server._scope_for_log({"scope": "project:explicit"}) == "project:explicit"
    assert mcp_server._scope_for_log({}) is None
