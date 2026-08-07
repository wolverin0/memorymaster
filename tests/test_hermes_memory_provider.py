from __future__ import annotations

import hashlib
import json
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest


PLUGIN_SRC = Path(__file__).parents[1] / "integrations" / "hermes-memorymaster" / "src"
if str(PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(PLUGIN_SRC))

from hermes_memorymaster.backend import (  # noqa: E402
    BackendAuthError,
    BackendTransientError,
)
from hermes_memorymaster.config import ProviderConfig  # noqa: E402
from hermes_memorymaster.outbox import DurableOutbox, OutboxFullError  # noqa: E402
from hermes_memorymaster.provider import MemoryMasterProvider  # noqa: E402


class FakeBackend:
    def __init__(self) -> None:
        self.remembered: list[dict[str, Any]] = []
        self.failures: list[Exception] = []
        self.recall_result = "authoritative context"
        self.recalls = 0
        self.scope_result: dict[str, Any] = {"scope": "user"}
        self.forget_result: dict[str, Any] = {"apply": False}
        self.improved: list[dict[str, Any]] = []

    def remember(self, envelope: dict[str, Any]) -> dict[str, Any]:
        if self.failures:
            raise self.failures.pop(0)
        self.remembered.append(envelope)
        return {"ok": True, "deduplicated": False}

    def recall(self, query: str, *, scope: str, session_id: str) -> str:
        if self.failures:
            raise self.failures.pop(0)
        self.recalls += 1
        return self.recall_result

    def scope(
        self,
        action: str,
        *,
        session_id: str,
        source_agent: str,
        platform: str,
        scope: str = "",
        task_label: str = "",
    ) -> dict[str, Any]:
        return {
            **self.scope_result,
            "action": action,
            "session_id": session_id,
            "platform": platform,
        }

    def forget_preview(
        self, *, claim_id: int = 0, source_item_id: int = 0
    ) -> dict[str, Any]:
        return self.forget_result

    def improve(self, *, scope: str, max_items: int = 200) -> dict[str, Any]:
        payload = {"scope": scope, "max_items": max_items}
        self.improved.append(payload)
        return {"ok": True, **payload}


class FakeReplica:
    def __init__(self, result: str = "replica context") -> None:
        self.result = result
        self.recalls = 0

    def recall(self, query: str, *, scope: str, session_id: str) -> str:
        self.recalls += 1
        return self.result


@pytest.fixture
def config(tmp_path: Path) -> ProviderConfig:
    return ProviderConfig(
        endpoint="http://127.0.0.1:8765/mcp",
        token="fixture-token",
        outbox_path=tmp_path / "outbox.db",
        worker_enabled=False,
        max_pending=10,
        max_pending_bytes=64 * 1024,
        shutdown_drain_seconds=0.05,
        retry_base_seconds=1.0,
        retry_cap_seconds=60.0,
        circuit_failure_threshold=2,
        circuit_reset_seconds=30.0,
    )


def _provider(config: ProviderConfig, backend: FakeBackend | None = None) -> MemoryMasterProvider:
    provider = MemoryMasterProvider(config=config, backend=backend or FakeBackend())
    provider.initialize(
        "raw-hermes-session",
        hermes_home=str(config.outbox_path.parent),
        platform="telegram",
        agent_context="primary",
        agent_identity="otacon",
    )
    return provider


def test_sync_turn_persists_before_return_without_calling_backend(config: ProviderConfig) -> None:
    backend = FakeBackend()
    provider = _provider(config, backend)

    started = time.perf_counter()
    provider.sync_turn(
        "Remember token=super-secret-value",
        "I will remember that.",
        session_id="raw-hermes-session",
    )

    assert time.perf_counter() - started < 0.2
    assert backend.remembered == []
    assert provider.status()["pending"] == 1
    payload = config.outbox_path.read_bytes()
    assert b"raw-hermes-session" not in payload
    assert b"super-secret-value" not in payload


def test_sync_turn_enqueue_p95_is_below_fifty_milliseconds(config: ProviderConfig) -> None:
    benchmark = replace(config, max_pending=100, max_pending_bytes=2 * 1024 * 1024)
    provider = _provider(benchmark)
    durations = []
    for turn in range(40):
        provider.on_turn_start(turn, "benchmark")
        started = time.perf_counter()
        provider.sync_turn(f"turn {turn}", "captured")
        durations.append(time.perf_counter() - started)

    p95 = statistics.quantiles(durations, n=20)[18]
    assert p95 < 0.05, p95


def test_drain_replays_exactly_once_and_duplicate_enqueue_is_idempotent(
    config: ProviderConfig,
) -> None:
    backend = FakeBackend()
    provider = _provider(config, backend)
    provider.sync_turn("Project deadline is Friday", "Understood.")
    provider.sync_turn("Project deadline is Friday", "Understood.")

    assert provider.status()["pending"] == 1
    assert provider.drain_once() is True
    assert provider.drain_once() is False
    assert len(backend.remembered) == 1
    assert provider.status()["completed"] == 1


def test_pending_entry_survives_process_reopen(config: ProviderConfig) -> None:
    provider = _provider(config)
    provider.sync_turn("Durable turn", "Durable response")
    provider.close_outbox()

    reopened = DurableOutbox(
        config.outbox_path,
        max_pending=config.max_pending,
        max_pending_bytes=config.max_pending_bytes,
    )
    assert reopened.counts()["pending"] == 1
    reopened.close()


def test_expired_lease_becomes_retryable_without_process_reopen(
    config: ProviderConfig,
) -> None:
    now = [100.0]
    outbox = DurableOutbox(
        config.outbox_path,
        max_pending=config.max_pending,
        max_pending_bytes=config.max_pending_bytes,
        clock=lambda: now[0],
    )
    outbox.enqueue("lease-key", {"operation": "remember", "payload": {"text": "x"}})
    first = outbox.lease_next(lease_seconds=5.0)
    assert first is not None

    now[0] = 106.0
    recovered = outbox.lease_next(lease_seconds=5.0)

    assert recovered is not None
    assert recovered.id == first.id
    assert recovered.attempts == 2
    outbox.close()


def test_transient_failure_retries_and_auth_failure_blocks(config: ProviderConfig) -> None:
    backend = FakeBackend()
    backend.failures = [BackendTransientError("network_down")]
    provider = _provider(config, backend)
    provider.sync_turn("Retry this", "Queued")

    assert provider.drain_once() is True
    assert provider.status()["retryable"] == 1
    provider.outbox.make_due_for_test()
    backend.failures = [BackendAuthError("unauthorized")]
    assert provider.drain_once() is True
    assert provider.status()["blocked"] == 1
    assert provider.status()["last_error_code"] == "unauthorized"


def test_queue_bounds_fail_closed(config: ProviderConfig) -> None:
    bounded = replace(
        config,
        max_pending=1,
        outbox_path=config.outbox_path.parent / "bounded.db",
    )
    provider = _provider(bounded)
    provider.sync_turn("first", "response")
    with pytest.raises(OutboxFullError, match="bounded"):
        provider.sync_turn("second", "response")


def test_non_primary_contexts_do_not_auto_write(config: ProviderConfig) -> None:
    for context in ("subagent", "cron", "flush"):
        provider = MemoryMasterProvider(config=config, backend=FakeBackend())
        provider.initialize(
            f"session-{context}",
            hermes_home=str(config.outbox_path.parent),
            platform="cron",
            agent_context=context,
        )
        provider.sync_turn("system task", "system result")
        assert provider.status()["pending"] == 0


def test_session_switch_updates_only_hashed_identity(config: ProviderConfig) -> None:
    provider = _provider(config)
    first = provider.session_hash
    provider.on_session_switch("different-raw-session", parent_session_id="raw-hermes-session")
    provider.sync_turn("new session turn", "response")

    assert provider.session_hash != first
    assert provider.session_hash == hashlib.sha256(b"different-raw-session").hexdigest()
    assert b"different-raw-session" not in config.outbox_path.read_bytes()


def test_session_switch_preserves_hashed_branch_lineage(config: ProviderConfig) -> None:
    provider = _provider(config)
    provider.on_session_switch(
        "child-session",
        parent_session_id="parent-session",
        rewound=True,
    )
    provider.sync_turn("branched turn", "response")

    entry = provider.outbox.peek_for_test()
    assert entry is not None
    lineage = entry.envelope["payload"]["metadata"]["session_lineage"]
    assert lineage == {
        "parent_session_hash": hashlib.sha256(b"parent-session").hexdigest(),
        "reset": False,
        "rewound": True,
    }
    serialized = json.dumps(entry.envelope)
    assert "parent-session" not in serialized
    assert "child-session" not in serialized


def test_prefetch_degrades_to_read_only_replica(config: ProviderConfig) -> None:
    authority = FakeBackend()
    authority.failures = [BackendTransientError("authority_offline")]
    replica = FakeReplica()
    provider = MemoryMasterProvider(config=config, backend=authority, replica_backend=replica)
    provider.initialize(
        "session",
        hermes_home=str(config.outbox_path.parent),
        platform="telegram",
        agent_context="primary",
    )

    assert provider.recall_now("where is it?") == "replica context"
    assert replica.recalls == 1
    provider.sync_turn("write while offline", "queued only")
    authority.failures = [BackendTransientError("authority_offline")]
    assert provider.drain_once() is True
    assert provider.status()["retryable"] == 1


def test_queue_prefetch_warms_fast_cache(config: ProviderConfig) -> None:
    backend = FakeBackend()
    provider = _provider(config, backend)

    provider.queue_prefetch("cached question")
    provider._prefetch_thread.join(timeout=1.0)

    assert provider.prefetch("cached question") == "authoritative context"
    assert backend.recalls == 1


def test_session_lifecycle_drains_turn_and_queues_improve_once(
    config: ProviderConfig,
) -> None:
    backend = FakeBackend()
    provider = _provider(config, backend)
    provider.sync_turn("durable lifecycle turn", "captured")

    provider.on_pre_compress([])
    provider.on_session_end([])
    provider.on_session_end([])

    assert len(backend.remembered) == 1
    assert backend.improved == [{"scope": "user", "max_items": 200}]
    assert provider.status()["completed"] == 2


def test_circuit_breaker_opens_after_bounded_transient_failures(
    config: ProviderConfig,
) -> None:
    backend = FakeBackend()
    backend.failures = [
        BackendTransientError("network_down"),
        BackendTransientError("network_down"),
    ]
    provider = _provider(config, backend)
    provider.on_turn_start(1, "first")
    provider.sync_turn("first", "response")
    provider.on_turn_start(2, "second")
    provider.sync_turn("second", "response")

    assert provider.drain_once() is True
    assert provider.drain_once() is True
    assert provider.status()["circuit_open"] is True
    assert provider.drain_once() is False


def test_transient_delivery_stops_after_five_attempts(config: ProviderConfig) -> None:
    bounded = replace(config, circuit_failure_threshold=99)
    backend = FakeBackend()
    backend.failures = [BackendTransientError("network_down") for _ in range(5)]
    provider = _provider(bounded, backend)
    provider.sync_turn("five attempts only", "queued")

    for _ in range(5):
        assert provider.drain_once() is True
        provider.outbox.make_due_for_test()

    assert provider.status()["blocked"] == 1
    assert provider.status()["last_error_code"] == "attempts_exhausted"


def test_tools_are_bounded_and_forget_is_preview_only(config: ProviderConfig) -> None:
    backend = FakeBackend()
    provider = _provider(config, backend)
    schemas = provider.get_tool_schemas()
    names = {item["name"] for item in schemas}

    assert names == {
        "memorymaster_recall",
        "memorymaster_remember",
        "memorymaster_scope",
        "memorymaster_forget_preview",
    }
    forget_schema = next(item for item in schemas if item["name"] == "memorymaster_forget_preview")
    assert "apply" not in forget_schema["parameters"]["properties"]
    result = json.loads(
        provider.handle_tool_call("memorymaster_forget_preview", {"claim_id": 7})
    )
    assert result["apply"] is False


def test_memory_write_remove_queues_preview_never_apply(config: ProviderConfig) -> None:
    provider = _provider(config)
    provider.on_memory_write(
        "remove",
        "memory",
        "claim:7",
        {"claim_id": 7, "session_id": "must-not-persist"},
    )

    entry = provider.outbox.peek_for_test()
    assert entry is not None
    assert entry.envelope["operation"] == "forget_preview"
    assert entry.envelope["payload"] == {"claim_id": 7, "source_item_id": 0}
    assert "must-not-persist" not in json.dumps(entry.envelope)


def test_shutdown_is_bounded_when_authority_is_slow(config: ProviderConfig) -> None:
    class SlowBackend(FakeBackend):
        def remember(self, envelope: dict[str, Any]) -> dict[str, Any]:
            time.sleep(0.25)
            return super().remember(envelope)

    provider = _provider(config, SlowBackend())
    provider.sync_turn("slow", "authority")
    started = time.perf_counter()
    provider.shutdown()
    assert time.perf_counter() - started < 0.2
