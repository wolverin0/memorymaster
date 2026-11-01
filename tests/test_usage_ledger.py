from __future__ import annotations

import pytest

from memorymaster.core import llm_provider
from memorymaster.core.usage_ledger import UsageLedger, UsageQuotaExceeded


def test_durable_quota_survives_new_ledger_instance(tmp_path) -> None:
    db = tmp_path / "usage.db"
    first = UsageLedger(db)
    first.reserve(
        operation="llm",
        provider="google",
        actor="account",
        units=1,
        global_limit=1,
    )

    second = UsageLedger(db)
    with pytest.raises(UsageQuotaExceeded):
        second.reserve(
            operation="llm",
            provider="google",
            actor="account",
            units=1,
            global_limit=1,
        )


def test_provider_and_actor_partitions_are_atomic(tmp_path) -> None:
    ledger = UsageLedger(tmp_path / "usage.db")
    ledger.reserve(
        operation="embedding",
        provider="gemini",
        actor="agent-a",
        units=2,
        provider_limit=3,
        actor_limit=2,
    )
    with pytest.raises(UsageQuotaExceeded):
        ledger.reserve(
            operation="embedding",
            provider="gemini",
            actor="agent-a",
            units=1,
            provider_limit=3,
            actor_limit=2,
        )
    # A rejected reservation does not consume provider capacity.
    ledger.reserve(
        operation="embedding",
        provider="gemini",
        actor="agent-b",
        units=1,
        provider_limit=3,
        actor_limit=2,
    )


def test_call_llm_blocks_before_provider_when_durable_cap_is_exhausted(
    tmp_path, monkeypatch
) -> None:
    db = tmp_path / "usage.db"
    monkeypatch.setenv("MEMORYMASTER_USAGE_LEDGER_DB", str(db))
    monkeypatch.setenv("MEMORYMASTER_MAX_LLM_CALLS_PER_DAY", "1")
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "google")
    calls: list[int] = []
    monkeypatch.setitem(
        llm_provider._PROVIDERS,
        "google",
        lambda _prompt, _text: calls.append(1) or "ok",
    )

    assert llm_provider.call_llm("p", "t") == "ok"
    with pytest.raises(Exception, match="durable_daily_exhausted"):
        llm_provider.call_llm("p", "t")
    assert len(calls) == 1


def test_mcp_quota_survives_process_local_bucket_reset(tmp_path, monkeypatch) -> None:
    import memorymaster.surfaces.mcp_server as mcp_server

    monkeypatch.setenv("MEMORYMASTER_USAGE_LEDGER_DB", str(tmp_path / "usage.db"))
    monkeypatch.setenv("MEMORYMASTER_MAX_MCP_INGESTS_PER_DAY", "1")
    monkeypatch.setenv("MM_INGEST_RATE_LIMIT_PER_MIN", "0")

    assert mcp_server._check_ingest_rate_limit("agent-a") is None
    mcp_server._INGEST_RATE_BUCKETS.clear()
    rejected = mcp_server._check_ingest_rate_limit("agent-b")

    assert rejected is not None
    assert rejected["code"] == "RATE_LIMITED"
    assert rejected["partition"] == "global"


def test_total_sums_units_per_operation_and_window(tmp_path) -> None:
    from memorymaster.core.usage_ledger import EMBED_DOC_OPERATION, EMBED_QUERY_OPERATION

    ledger = UsageLedger(tmp_path / "usage.db")
    assert ledger.total(EMBED_QUERY_OPERATION) == 0
    ledger.reserve(operation=EMBED_QUERY_OPERATION, provider="gemini", actor="a", units=7)
    ledger.reserve(operation=EMBED_QUERY_OPERATION, provider="gemini", actor="a", units=5)
    ledger.reserve(operation=EMBED_DOC_OPERATION, provider="gemini", actor="a", units=100)
    ledger.reserve(
        operation=EMBED_QUERY_OPERATION, provider="gemini", actor="a", units=9, window_key="2020-01-01"
    )
    assert ledger.total(EMBED_QUERY_OPERATION) == 12
    assert ledger.total(EMBED_DOC_OPERATION) == 100
    assert ledger.total(EMBED_QUERY_OPERATION, window_key="2020-01-01") == 9


def test_embed_token_limits_default_override_and_zero_is_a_zero_budget(monkeypatch) -> None:
    from memorymaster.core.usage_ledger import (
        EMBED_DOC_OPERATION,
        EMBED_QUERY_OPERATION,
        embed_token_limit,
    )

    q_env = "MEMORYMASTER_MAX_EMBED_QUERY_TOKENS_PER_DAY"
    monkeypatch.delenv(q_env, raising=False)
    monkeypatch.delenv("MEMORYMASTER_MAX_EMBED_DOC_TOKENS_PER_DAY", raising=False)
    assert embed_token_limit(EMBED_QUERY_OPERATION) == 1_000_000
    assert embed_token_limit(EMBED_DOC_OPERATION) == 1_500_000
    monkeypatch.setenv(q_env, "250")
    assert embed_token_limit(EMBED_QUERY_OPERATION) == 250
    monkeypatch.setenv(q_env, "0")
    assert embed_token_limit(EMBED_QUERY_OPERATION) == 0
    monkeypatch.setenv(q_env, "lots")
    assert embed_token_limit(EMBED_QUERY_OPERATION) == 1_000_000


def test_embed_caps_do_not_enable_the_legacy_configured_ledger(monkeypatch) -> None:
    from memorymaster.core.usage_ledger import configured_ledger

    for name in (
        "MEMORYMASTER_USAGE_LEDGER_DB",
        "MEMORYMASTER_MAX_LLM_CALLS_PER_DAY",
        "MEMORYMASTER_MAX_PROVIDER_CALLS_PER_DAY",
        "MEMORYMASTER_MAX_EMBEDDING_ITEMS_PER_DAY",
        "MEMORYMASTER_MAX_MCP_INGESTS_PER_DAY",
        "MEMORYMASTER_MAX_MCP_INGESTS_PER_AGENT_PER_DAY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMORYMASTER_MAX_EMBED_QUERY_TOKENS_PER_DAY", "5")
    assert configured_ledger() is None  # reserve_configured behaviour is untouched
