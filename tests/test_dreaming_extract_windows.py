"""T-1864: windowed extraction with a persisted cursor (F3) and append-during-call safety (F12)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memorymaster.dreaming.capture import parse_transcript_lines
from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.models import (
    CaptureEnvelope,
    ConsolidationResult,
    DreamMessage,
    ExtractionResult,
    ProviderUsage,
)
from memorymaster.dreaming.worker import DreamConfig, DreamWorker

NOW = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
_USAGE = ProviderUsage("stub", "stub-model", 200, 10, 20, 5, True)


class _Recorder:
    """Extractor stub that records every message text it is sent; never calls a provider."""

    provider = "stub"
    model = "stub-model"
    uncapped_daily = True

    def __init__(self, during_call=None) -> None:
        self.calls: list[list[str]] = []
        self.during_call = during_call

    def extract(self, messages, *, scope, capture_hash):
        self.calls.append([str(message["text"]) for message in messages])
        if self.during_call is not None:
            hook, self.during_call = self.during_call, None
            hook()
        return ExtractionResult((), _USAGE)

    @property
    def seen(self) -> str:
        return "\n".join(text for call in self.calls for text in call)


class _Consolidator:
    provider = "stub"
    model = "stub-model"

    def consolidate(self, candidates, current_claims, *, scope):
        return ConsolidationResult((), _USAGE)


def _envelope(texts, *, start=0, end=100, content_hash="h0") -> CaptureEnvelope:
    stamp = (NOW - timedelta(hours=1)).isoformat()
    messages = tuple(
        DreamMessage(f"m{start}-{index}", "user" if index % 2 == 0 else "assistant", text, stamp)
        for index, text in enumerate(texts)
    )
    return CaptureEnvelope(
        provider="codex", session_hash="s", scope="project:test", captured_at=NOW.isoformat(),
        last_activity_at=stamp, messages=messages, cursor_start=start, cursor_end=end,
        content_hash=content_hash,
    )


def _worker(ledger, extractor, **config) -> DreamWorker:
    return DreamWorker(ledger, None, extractor, _Consolidator(),
                       config=DreamConfig(idle_minutes=30, **config), now=lambda: NOW)


def test_omitted_prefix_is_extracted_before_the_capture_is_marked_extracted(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "w.db")
    texts = [f"{label} " + "x" * 55 for label in ("PREFIXFACT", "MIDDLEFACT", "SUFFIXFACT")]
    capture_id = ledger.enqueue(_envelope(texts))
    extractor = _Recorder()
    worker = _worker(ledger, extractor, max_input_chars=100)

    worker.run(apply_candidates=False)

    for label in ("PREFIXFACT", "MIDDLEFACT", "SUFFIXFACT"):
        assert label in extractor.seen, f"{label} was never sent to the extractor"
    assert len(extractor.calls) >= 2
    assert ledger.get_capture(capture_id)["state"] != "captured"


def test_message_tail_beyond_16000_chars_is_kept_and_extracted(tmp_path: Path) -> None:
    long_text = "A" * 16_100 + " TAILFACT durable decision"
    line = json.dumps({"type": "user", "uuid": "u1", "message": {"role": "user", "content": long_text}})
    messages = parse_transcript_lines(line, "claude")
    assert "TAILFACT" in "".join(message.text for message in messages)

    ledger = DreamLedger(tmp_path / "w.db")
    capture_id = ledger.enqueue(_envelope([m.text for m in messages] + ["ok"]))
    extractor = _Recorder()
    _worker(ledger, extractor, max_input_chars=20_000).run(apply_candidates=False)
    assert "TAILFACT" in extractor.seen
    assert ledger.get_capture(capture_id)["state"] != "captured"


def test_message_appended_during_the_provider_call_is_extracted_on_the_next_run(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "w.db")
    capture_id = ledger.enqueue(_envelope(["First message here.", "Second message here."]))

    def append_decision() -> None:
        later = _envelope(["We decided DECISIONFACT is the plan."], start=100, end=200, content_hash="h1")
        assert ledger.enqueue(later) == capture_id  # coalesced into the in-flight capture

    extractor = _Recorder(during_call=append_decision)
    _worker(ledger, extractor).run(apply_candidates=False)
    assert "DECISIONFACT" not in extractor.seen
    row = ledger.get_capture(capture_id)
    assert row["turn_count"] == 3
    assert row["state"] in {"captured", "retryable"}  # not consumed: it still has an unextracted tail

    DreamWorker(ledger, None, extractor, _Consolidator(), config=DreamConfig(idle_minutes=30),
                now=lambda: NOW + timedelta(hours=2)).run(apply_candidates=False)
    assert "DECISIONFACT" in extractor.seen
    assert ledger.get_capture(capture_id)["state"] != "captured"


def test_extracted_capture_is_never_coalesced_into(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "w.db")
    capture_id = ledger.enqueue(_envelope(["First message here.", "Second message here."]))
    _worker(ledger, _Recorder()).run(apply_candidates=False)
    assert ledger.get_capture(capture_id)["state"] != "captured"
    later = ledger.enqueue(_envelope(["Third message here."], start=100, end=200, content_hash="h1"))
    assert later != capture_id
    assert ledger.get_capture(capture_id)["turn_count"] == 2


def test_migration_adds_the_cursor_column_and_leaves_history_untouched(tmp_path: Path) -> None:
    path = tmp_path / "w.db"
    ledger = DreamLedger(path)
    capture_id = ledger.enqueue(_envelope(["First message here.", "Second message here."]))
    ledger.set_extraction(capture_id, [], "fixture")  # a historical, fully extracted row
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE dream_captures DROP COLUMN extracted_through")
        conn.execute("DELETE FROM dream_schema_versions WHERE version = 5")
    DreamLedger(path)
    with sqlite3.connect(path) as conn:
        assert 5 in {row[0] for row in conn.execute("SELECT version FROM dream_schema_versions")}
    row = DreamLedger(path).get_capture(capture_id)
    assert row["extracted_through"] is None and row["state"] == "extracted"


def test_input_cap_below_the_unit_size_is_clamped_and_logged(monkeypatch, caplog) -> None:
    """T-1877 (review N11): a 100-char cap cannot yield 100-char windows from 16,000-char units; say so."""
    import logging

    from memorymaster.dreaming.extraction_window import PIECE_CHARS, next_window

    monkeypatch.setenv("MEMORYMASTER_DREAM_MAX_INPUT_CHARS", "100")
    with caplog.at_level(logging.WARNING, logger="memorymaster.dreaming.worker"):
        config = DreamConfig.from_env()
    assert config.max_input_chars == PIECE_CHARS
    assert "MEMORYMASTER_DREAM_MAX_INPUT_CHARS" in caplog.text
    window, cursor = next_window([{"text": "y" * 20_000}], 0, config.max_input_chars)
    assert cursor == 1 and len(window[0]["text"]) <= config.max_input_chars  # the effective cap holds

    monkeypatch.setenv("MEMORYMASTER_DREAM_MAX_INPUT_CHARS", "40000")
    assert DreamConfig.from_env().max_input_chars == 40_000  # a sane value is left alone
