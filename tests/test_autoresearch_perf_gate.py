"""The perf gate measures external calls instead of reporting a constant, and runs hermetically.

Found 2026-10-04 (autoresearch first run): `provider_calls` was a literal 0 and the gate
only removed QDRANT_URL and the rerank flags. On the operator's machine the shell
carries Jev in live mode and the real home, so a "benchmark" could reach providers
and write the real decisions ledger, and still report zero calls.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / "scripts" / "autoresearch_perf_gate.py"


def _gate():
    spec = importlib.util.spec_from_file_location("perf_gate_under_test", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sample() -> dict:
    timing = {"p95_seconds": 0.01, "throughput_ops_per_sec": 100.0, "misses": 0}
    return {"timing": {"query": timing, "ingest": timing, "cycle": timing, "total_runtime_seconds": 1.0},
            "quality": {"confirmed_claims_after_cycles": 3}}


def _run(gate, monkeypatch, capsys, smoke, *args: str) -> tuple[int, dict]:
    monkeypatch.setattr(gate, "_load_perf_smoke", lambda: type("Smoke", (), {"run_perf_smoke": staticmethod(smoke)}))
    monkeypatch.setattr(sys, "argv", ["gate", "--claims", "3", "--queries", "2", "--repeats", "1", *args])
    code = gate.main()
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_an_outbound_connection_is_blocked_counted_and_fails_the_gate(monkeypatch, capsys):
    def smoke(**_kwargs):
        with pytest.raises(OSError):
            socket.create_connection(("203.0.113.7", 443), timeout=1)  # TEST-NET-3, never routed
        return _sample()

    code, out = _run(_gate(), monkeypatch, capsys, smoke)
    assert (code, out["provider_calls"], out["verdict"]) == (1, 1, "fail")
    assert out["violations"] == ["provider_calls 1 > max 0"]


def test_loopback_stays_allowed_and_a_clean_run_passes(monkeypatch, capsys):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def smoke(**_kwargs):
        socket.create_connection(server.getsockname(), timeout=1).close()
        return _sample()

    try:
        code, out = _run(_gate(), monkeypatch, capsys, smoke)
    finally:
        server.close()
    assert (code, out["provider_calls"], out["verdict"]) == (0, 0, "pass")


def test_the_live_configuration_and_real_home_never_reach_the_benchmark(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("MEMORYMASTER_JEV_MODE", "live")
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(tmp_path / "real-ledger.db"))
    seen: dict = {}

    def smoke(**_kwargs):
        seen.update(os.environ)
        seen["home"] = str(Path.home())
        return _sample()

    _run(_gate(), monkeypatch, capsys, smoke)
    assert "MEMORYMASTER_JEV_MODE" not in seen and "GEMINI_API_KEY" not in seen
    assert "mm-autoresearch-perf-" in seen["home"]
    assert "mm-autoresearch-perf-" in seen["MEMORYMASTER_DECISIONS_DB"]
    assert os.environ["MEMORYMASTER_JEV_MODE"] == "live"  # restored afterwards
