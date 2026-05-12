from __future__ import annotations

from dataclasses import dataclass

import pytest

import memorymaster.mcp_server as mcp_server


@dataclass
class FakeClock:
    now: float = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def reset_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp_server._INGEST_RATE_BUCKETS.clear()
    monkeypatch.delenv("MM_INGEST_RATE_LIMIT_PER_MIN", raising=False)


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(mcp_server, "_monotonic", clock.monotonic)
    return clock


@pytest.fixture
def mcp_db(tmp_path):
    if not hasattr(mcp_server, "ingest_claim") or not hasattr(mcp_server, "init_db"):
        pytest.skip("MCP not installed")
    db_path = str(tmp_path / "rate-limit.db")
    workspace = str(tmp_path)
    mcp_server.init_db(db=db_path, workspace=workspace)
    return db_path, workspace


def _ingest(index: int, db_path: str, workspace: str, source_agent: str) -> dict:
    return mcp_server.ingest_claim(
        text=f"Rate limit test claim {source_agent} {index}",
        db=db_path,
        workspace=workspace,
        sources_json='["test_mcp_rate_limit.py"]',
        source_agent=source_agent,
    )


def test_under_limit_succeeds(mcp_db, fake_clock: FakeClock) -> None:
    db_path, workspace = mcp_db

    results = [_ingest(i, db_path, workspace, "under-limit-agent") for i in range(10)]

    assert all(result["ok"] is True for result in results)


def test_over_limit_returns_error(mcp_db, fake_clock: FakeClock) -> None:
    db_path, workspace = mcp_db

    results = [_ingest(i, db_path, workspace, "limited-agent") for i in range(100)]

    assert all(result["ok"] is True for result in results[:60])
    assert all(result["ok"] is False for result in results[60:])
    assert {result["code"] for result in results[60:]} == {"RATE_LIMITED"}
    assert all(result["retry_after_ms"] > 0 for result in results[60:])


def test_per_agent_isolation(mcp_db, fake_clock: FakeClock) -> None:
    db_path, workspace = mcp_db

    agent_a_results = [_ingest(i, db_path, workspace, "agent-a") for i in range(100)]
    agent_b_result = _ingest(0, db_path, workspace, "agent-b")

    assert any(result["ok"] is False for result in agent_a_results)
    assert agent_b_result["ok"] is True


def test_refill(mcp_db, fake_clock: FakeClock) -> None:
    db_path, workspace = mcp_db
    for i in range(60):
        assert _ingest(i, db_path, workspace, "refill-agent")["ok"] is True
    assert _ingest(60, db_path, workspace, "refill-agent")["code"] == "RATE_LIMITED"

    fake_clock.advance(1.0)

    assert _ingest(61, db_path, workspace, "refill-agent")["ok"] is True
    assert _ingest(62, db_path, workspace, "refill-agent")["code"] == "RATE_LIMITED"


def test_disabled_via_env(mcp_db, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path, workspace = mcp_db
    monkeypatch.setenv("MM_INGEST_RATE_LIMIT_PER_MIN", "0")

    results = [_ingest(i, db_path, workspace, "disabled-agent") for i in range(100)]

    assert all(result["ok"] is True for result in results)
