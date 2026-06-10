"""Chaos-soak writer roles + acked-op ledger (spec §4; see chaos_soak.py).

Extracted from chaos_soak.py to keep that file under the 800-LOC boundary.
These loops run inside the killed-and-respawned subprocesses that
``chaos_soak.py --role <name>`` spawns; each one mirrors a member of the real
~12-process writer fleet at the SERVICE layer (svc.ingest / query_for_context /
store_verbatim / spool.append / run_cycle / merge_databases) so the soak
exercises the exact code paths production exercises — including the flag
branch each writer takes under MEMORYMASTER_WAL_DISCIPLINE.
"""
from __future__ import annotations

import json
import os
import random
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

RECALL_QUERIES = (
    "sqlite wal checkpoint discipline",
    "soak claim writer kill",
    "btree corruption recovery",
)


class Ledger:
    """Append-only acked-op journal, one file per writer spawn.

    An op is journaled ONLY AFTER the DB commit / spool append returned —
    so "in the ledger" == "the writer was told the write is durable". A kill
    between commit and journal loses the ledger line, never the write, which
    keeps the reconciliation one-sided (no false lost-write reports).
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8")

    def _write(self, record: dict[str, object]) -> None:
        record["ts"] = datetime.now(timezone.utc).isoformat()
        self._fh.write(json.dumps(record, ensure_ascii=True) + "\n")
        self._fh.flush()

    def ack(self, kind: str, key: str | None, **extra: object) -> None:
        self._write({"kind": kind, "key": key, **extra})

    def error(self, exc: Exception) -> None:
        detail = str(exc).lower()
        busy = "locked" in detail or "busy" in detail
        record: dict[str, object] = {"kind": "busy" if busy else "error", "detail": str(exc)[:300]}
        if not busy:
            # Busy errors are expected contention; anything else carries the
            # raising frames so a failed gate run is diagnosable from ledgers.
            tb = traceback.extract_tb(exc.__traceback__)[-4:]
            record["trace"] = [f"{f.filename}:{f.lineno}:{f.name}" for f in tb]
        self._write(record)


def _unique(prefix: str, writer_id: int, seq: int) -> str:
    return f"soak-{prefix}-{writer_id}-{os.getpid()}-{seq}"


def _writer_ingest(db: Path, ledger: Ledger, writer_id: int) -> None:
    """6x MCP-style ingest: fresh MemoryService per batch (mcp_server pattern)."""
    from memorymaster.models import CitationInput
    from memorymaster.service import MemoryService

    seq = 0
    while True:
        svc = MemoryService(db)
        for _ in range(5):
            seq += 1
            key = _unique("ingest", writer_id, seq)
            text = f"Soak claim {key}: sqlite wal checkpoint discipline survives a hard writer kill."
            try:
                svc.ingest(
                    text,
                    [CitationInput(source="chaos-soak", locator=key)],
                    idempotency_key=key,
                    scope="project:soak",
                    source_agent="chaos-soak",
                )
            except Exception as exc:  # noqa: BLE001 - soak writers must survive anything
                ledger.error(exc)
                continue
            ledger.ack("ingest", key)
            time.sleep(random.uniform(0.02, 0.15))


def _writer_recall(db: Path, ledger: Ledger, writer_id: int) -> None:
    """2x per-prompt recall: query_for_context with access recording on.

    Flag ON -> read_only store (access/feedback lines spool, spec §2.2);
    flag OFF -> legacy direct-UPDATE writer. Mirrors context_hook exactly at
    the service layer.
    """
    from memorymaster import spool
    from memorymaster.service import MemoryService

    read_only = spool.wal_discipline_enabled()
    while True:
        svc = MemoryService(db, read_only=read_only)
        try:
            svc.query_for_context(random.choice(RECALL_QUERIES), token_budget=800, limit=20)
        except Exception as exc:  # noqa: BLE001
            ledger.error(exc)
        else:
            ledger.ack("recall", None)
        time.sleep(random.uniform(0.05, 0.25))


def _writer_stophook(db: Path, ledger: Ledger, writer_id: int) -> None:
    """1x Stop-hook: verbatim turn + learning ingest per iteration."""
    from memorymaster import spool
    from memorymaster.models import CitationInput
    from memorymaster.service import MemoryService
    from memorymaster.verbatim_store import ensure_verbatim_schema, store_verbatim

    session = f"soak-session-{writer_id}-{os.getpid()}"
    seq = 0
    use_spool = spool.wal_discipline_enabled()
    if not use_spool:
        ensure_verbatim_schema(str(db))
    while True:
        seq += 1
        key = _unique("stop", writer_id, seq)
        turn = f"Soak verbatim turn {key}: the user asked about WAL discipline and the agent answered at length."
        learning = f"Soak learning {key}: a kill -9 mid-commit must never corrupt the verbatim btree."
        try:
            if use_spool:
                spool.append(
                    db,
                    "verbatim",
                    {
                        "session_id": session,
                        "role": "assistant",
                        "content": turn,
                        "scope": "project:soak",
                        "source_agent": "chaos-soak",
                    },
                )
                ledger.ack("verbatim", key, session_id=session, content=turn)
                spool.append(
                    db,
                    "ingest",
                    {
                        "text": learning,
                        "citations": [{"source": "chaos-soak", "locator": key}],
                        "scope": "project:soak",
                        "source_agent": "chaos-soak",
                    },
                    idempotency_key=key,
                )
                ledger.ack("ingest", key)
            else:
                row_id = store_verbatim(
                    str(db), session, "assistant", turn,
                    scope="project:soak", source_agent="chaos-soak",
                )
                if row_id is not None:
                    ledger.ack("verbatim", key, session_id=session, content=turn)
                MemoryService(db).ingest(
                    learning,
                    [CitationInput(source="chaos-soak", locator=key)],
                    idempotency_key=key,
                    scope="project:soak",
                    source_agent="chaos-soak",
                )
                ledger.ack("ingest", key)
        except Exception as exc:  # noqa: BLE001
            ledger.error(exc)
        time.sleep(random.uniform(0.1, 0.4))


def _writer_dream(db: Path, ledger: Ledger, writer_id: int) -> None:
    """1x dream-bridge: op:"dream" envelopes under flag, direct ingest off-flag."""
    from memorymaster import spool
    from memorymaster.models import CitationInput
    from memorymaster.service import MemoryService

    seq = 0
    use_spool = spool.wal_discipline_enabled()
    while True:
        seq += 1
        key = _unique("dream", writer_id, seq)
        text = f"Soak dream item {key}: ambient memory absorbed from an auto-dream markdown note."
        try:
            if use_spool:
                spool.append(
                    db,
                    "dream",
                    {
                        "text": text,
                        "citations": [{"source": "dream-bridge", "locator": key}],
                        "scope": "project:soak",
                    },
                    idempotency_key=key,
                )
            else:
                MemoryService(db).ingest(
                    text,
                    [CitationInput(source="dream-bridge", locator=key)],
                    idempotency_key=key,
                    scope="project:soak",
                    source_agent="dream-bridge",
                )
            ledger.ack("ingest", key)
        except Exception as exc:  # noqa: BLE001
            ledger.error(exc)
        time.sleep(random.uniform(0.2, 0.6))


def _writer_steward(db: Path, ledger: Ledger, writer_id: int) -> None:
    """1x steward: run_cycle (small batch) — includes integrity + spool drain."""
    from memorymaster.service import MemoryService

    while True:
        try:
            MemoryService(db).run_cycle(batch_limit=25)
        except Exception as exc:  # noqa: BLE001
            ledger.error(exc)
        else:
            ledger.ack("cycle", None)
        time.sleep(3.0)


def _writer_merge(db: Path, ledger: Ledger, writer_id: int, sibling: Path) -> None:
    """1x merge-db: seed a sibling DB, merge it over the soak DB (hermes shape)."""
    from memorymaster.db_merge import merge_databases
    from memorymaster.models import CitationInput
    from memorymaster.service import MemoryService

    sib = MemoryService(sibling)
    if not sibling.exists():
        sib.init_db()
    seq = 0
    while True:
        seq += 1
        key = _unique("merge", writer_id, seq)
        text = f"Soak merged claim {key}: hermes delta sibling row survives concurrent merge."
        try:
            sib.ingest(
                text,
                [CitationInput(source="chaos-soak-merge", locator=key)],
                idempotency_key=key,
                scope="project:soak-merge",
                source_agent="chaos-soak",
            )
            merge_databases(str(db), str(sibling))
        except Exception as exc:  # noqa: BLE001
            ledger.error(exc)
        else:
            ledger.ack("merge", key, text=text)
        time.sleep(random.uniform(0.5, 1.5))


ROLE_LOOPS = {
    "ingest": _writer_ingest,
    "recall": _writer_recall,
    "stophook": _writer_stophook,
    "dream": _writer_dream,
    "steward": _writer_steward,
    "merge": _writer_merge,
}
