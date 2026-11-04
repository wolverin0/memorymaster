from __future__ import annotations

from pathlib import Path

from memorymaster.core.capture_control import CaptureLedger, CaptureLimits


def test_cursor_is_restart_safe_and_only_returns_complete_appends(tmp_path: Path) -> None:
    state = tmp_path / "capture.db"
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(b'{"turn": 1}\n')

    first = CaptureLedger(state).read_increment(transcript, "session:end")
    assert first.text == '{"turn": 1}\n'
    CaptureLedger(state).commit_cursor(first)
    assert CaptureLedger(state).read_increment(transcript, "session:end").text == ""

    with transcript.open("ab") as handle:
        handle.write(b'{"turn": 2}')
    assert CaptureLedger(state).read_increment(transcript, "session:end").text == ""
    with transcript.open("ab") as handle:
        handle.write(b"\n")
    second = CaptureLedger(state).read_increment(transcript, "session:end")
    assert second.text == '{"turn": 2}\n'
    CaptureLedger(state).commit_cursor(second)

    transcript.write_bytes(b'{"turn": "replacement"}\n')
    replacement = CaptureLedger(state).read_increment(transcript, "session:end")
    assert replacement.start_offset == 0
    assert "replacement" in replacement.text


def test_provider_global_and_session_budgets_survive_restart(tmp_path: Path) -> None:
    state = tmp_path / "capture.db"
    limits = CaptureLimits(global_daily_calls=3, provider_daily_calls=2, session_daily_calls=1)
    ledger = CaptureLedger(state, limits=limits)

    first = ledger.reserve_llm("google", "session-a", "session-end")
    assert first is not None
    ledger.finish_llm(first, input_bytes=40, output_bytes=20, outcome="ok")
    assert CaptureLedger(state, limits=limits).reserve_llm("google", "session-a", "retry") is None

    second = CaptureLedger(state, limits=limits).reserve_llm("google", "session-b", "session-end")
    assert second is not None
    assert CaptureLedger(state, limits=limits).reserve_llm("google", "session-c", "session-end") is None

    third = CaptureLedger(state, limits=limits).reserve_llm("openai", "session-c", "session-end")
    assert third is not None
    assert CaptureLedger(state, limits=limits).reserve_llm("openai", "session-d", "session-end") is None

    usage = CaptureLedger(state, limits=limits).usage()
    assert usage["global_calls"] == 3
    assert usage["providers"] == {"google": 2, "openai": 1}
    assert usage["input_bytes"] == 40
    assert usage["output_bytes"] == 20


def test_max_bytes_bounds_each_read_and_the_rest_drains_on_later_calls(tmp_path: Path) -> None:
    state = tmp_path / "capture.db"
    transcript = tmp_path / "session.jsonl"
    lines = [f'{{"turn": {i}}}\n' for i in range(10)]
    transcript.write_bytes("".join(lines).encode())
    cap = len(lines[0].encode()) * 3 + 2  # three whole lines plus a fragment of the fourth

    seen: list[str] = []
    for _ in range(10):
        chunk = CaptureLedger(state).read_increment(transcript, "stop-verbatim", max_bytes=cap)
        if not chunk.text:
            break
        assert len(chunk.text.encode()) <= cap and chunk.text.endswith("\n")
        seen.append(chunk.text)
        CaptureLedger(state).commit_cursor(chunk)

    assert len(seen) == 4 and "".join(seen) == "".join(lines)  # nothing lost, nothing repeated


def test_a_single_line_longer_than_the_cap_is_returned_whole(tmp_path: Path) -> None:
    state = tmp_path / "capture.db"
    transcript = tmp_path / "session.jsonl"
    big = '{"turn": "' + "x" * 500 + '"}\n'
    transcript.write_bytes((big + '{"turn": 2}\n').encode())

    first = CaptureLedger(state).read_increment(transcript, "k", max_bytes=100)

    assert first.text == big
    CaptureLedger(state).commit_cursor(first)
    assert CaptureLedger(state).read_increment(transcript, "k", max_bytes=100).text == '{"turn": 2}\n'


def test_without_max_bytes_the_whole_backlog_is_still_returned(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(b'{"a": 1}\n{"a": 2}\n')
    assert CaptureLedger(tmp_path / "c.db").read_increment(transcript, "k").text == '{"a": 1}\n{"a": 2}\n'
